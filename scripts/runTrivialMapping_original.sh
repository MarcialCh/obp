#!/usr/bin/env bash
# Compare pricing methods on freshly loaded datasets, one size at a time.
# Run from this artifact. This REBUILDS the configured experimental DB.
set -euo pipefail

# Edit these arrays, or override them with the command-line / DATA_SIZES below.
SCENARIOS=("scenarios/Deep100")
SIZES=("small" "medium")

export PRICING_METHODS="${PRICING_METHODS:-KCPrice,SATPrice,SATPrice-NoReduction,Baseline}"
export NUM_TESTS="${NUM_TESTS:-5}"
export WARMUP_RUNS="${WARMUP_RUNS:-1}"
export REUSE_SQL="${REUSE_SQL:-0}"
export CONTINUE_ON_ERROR="${CONTINUE_ON_ERROR:-1}"
export QUERIES="${QUERIES:-1,2,3,4,5}"
export SOURCE_KEY_MODE="${SOURCE_KEY_MODE:-strict}"
export SOURCE_TEXT_MODE="${SOURCE_TEXT_MODE:-strict}"

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  cat <<'HELP'
Usage: bash scripts/runTrivialMapping_original.sh [SCENARIO [QUERIES]]
  DATA_SIZES="small medium large huge"   overrides the SIZES array
  PRICING_METHODS=KCPrice,SATPrice,SATPrice-NoReduction,Baseline
  NUM_TESTS=5 WARMUP_RUNS=1 REUSE_SQL=0 CONTINUE_ON_ERROR=1
  PYTHON=python C2D_BIN=/path/to/c2d PRICE_TIMEOUT=100
  SOURCE_KEY_MODE=strict|surrogate  default: strict
  SOURCE_TEXT_MODE=strict|unbounded  default: strict
  PRICE_MODE=all|uniform|random|skewed|heavy-tailed PRICE_SEED=2026 PRICE_DECIMALS=3
  Defaults: all four price modes; each size is loaded only once
  PRICE_SKEW_PROB=0.9 PRICE_SKEW_CUTOFF=1 PRICE_PARETO_ALPHA=1.5 PRICE_PARETO_MIN=1

For each size: rebuild DB -> load that size's CSVs -> prepare ProvSQL -> price.
All selected CSV directories must already exist. This does not generate data.
DB configuration comes from SCENARIO/postgres-config.ini, using assignment-style connection parameters.
The configured experimental database is replaced; the final size stays loaded.
Do not run other initialization/pricing scripts against that DB concurrently.
CONTINUE_ON_ERROR applies to pricing failures only; setup failures always stop.
surrogate: replace source primary keys with __fact_id; retain all CSV values/rows.
Old primary-key indexes become non-unique; other constraints remain enforced.
unbounded: widen built-in varchar(n)/char(n) source columns to text before COPY.
Results: experiments/pricing/SCENARIO/SIZE/Qn/<batch>/
Load audit: experiments/pricing/SCENARIO/SIZE/loads/<batch>/load_manifest.json
HELP
  exit 0
fi
if (( $# > 2 )); then
  echo 'ERROR: expected [SCENARIO [QUERIES]]; set sizes via DATA_SIZES or SIZES.' >&2
  exit 2
fi
[[ -z "${DATA_SIZES:-}" ]] || read -r -a SIZES <<< "$DATA_SIZES"
(( $# < 1 )) || SCENARIOS=("$1")
(( $# < 2 )) || export QUERIES="$2"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
# Use one Python interpreter for loading, ProvSQL preparation and pricing.
exec "${PYTHON:-python}" -u - "$ROOT_DIR" "${#SCENARIOS[@]}" "${SCENARIOS[@]}" "${SIZES[@]}" <<'PYTHON'
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from types import SimpleNamespace


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')


def save_manifest(folder, data):
    temporary = folder / 'load_manifest.json.tmp'
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(folder / 'load_manifest.json')


class HashedReader:
    """Fingerprint exactly the CSV bytes streamed to COPY; no extra file scan."""
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.byte_count = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        self.byte_count += len(data)
        return data

    def readline(self, size=-1):
        data = self.stream.readline(size)
        self.digest.update(data)
        self.byte_count += len(data)
        return data


def use_surrogate_key(cur, sql, table_name):
    """Replace only this CSV-listed table's PK, retaining its lookup index.

    Called on a freshly created table, after __fact_id is available. Read the
    catalog instead of editing schema SQL; quoted names and composite keys work.
    RESTRICT ensures foreign keys/dependencies cannot be silently removed.
    """
    table = sql.Identifier('public', table_name)
    cur.execute("""
        SELECT c.conname, pg_catalog.pg_get_constraintdef(c.oid),
               pg_catalog.pg_get_indexdef(c.conindid),
               ARRAY(SELECT a.attname
                     FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, pos)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid=c.conrelid AND a.attnum=k.attnum
                     ORDER BY k.pos)
        FROM pg_catalog.pg_constraint c
        JOIN pg_catalog.pg_class t ON t.oid=c.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=%s AND t.relname=%s AND c.contype='p'
        ORDER BY c.conname
    """, ('public', table_name))
    primary_keys = cur.fetchall()
    require(len(primary_keys) <= 1, 'Multiple primary keys for public.' + table_name)
    audit = dict(identity_column='__fact_id', replaced_primary_key=None,
                 retained_lookup_index_sql=None)
    if primary_keys:
        name, definition, index_definition, columns = primary_keys[0]
        if list(columns) == ['__fact_id']:
            audit['identity_constraint'] = name
            return audit
        prefix = 'CREATE UNIQUE INDEX '
        require(index_definition.startswith(prefix),
                'Unsupported primary-key index definition for public.' + table_name)
        nonunique = 'CREATE INDEX ' + index_definition[len(prefix):]
        try:
            cur.execute(sql.SQL('ALTER TABLE {} DROP CONSTRAINT {} RESTRICT').format(
                table, sql.Identifier(name)))
        except Exception as exc:
            if getattr(exc, 'pgcode', None) == '2BP01':
                raise RuntimeError('Cannot replace primary key public.%s.%s: dependent '
                                   'objects (such as foreign keys) must be reviewed; '
                                   'SOURCE_KEY_MODE does not remove them' % (table_name, name)) from exc
            raise
        cur.execute(nonunique)
        audit.update(replaced_primary_key=dict(name=name, definition=definition,
                                              columns=list(columns)),
                     retained_lookup_index_sql=nonunique)
    # Match the installed KCPrice adapter's identity index name so prepare()
    # reuses this unique index instead of creating another one on __fact_id.
    identity = 'kcprice_id_' + hashlib.sha256(('public.' + table_name).encode()).hexdigest()[:20]
    cur.execute(sql.SQL('ALTER TABLE {} ADD CONSTRAINT {} PRIMARY KEY (__fact_id)').format(
        table, sql.Identifier(identity)))
    audit['identity_constraint'] = identity
    return audit


def widen_source_text(cur, sql, table_name, source_columns):
    """Remove scalar built-in string length limits before any CSV values enter.

    Domains, numeric/date types, metadata columns and other constraints stay as
    defined. Preserve column collations explicitly, including custom collations.
    """
    cur.execute("""
        SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod),
               cn.nspname, co.collname
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class t ON t.oid=a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_catalog.pg_type ty ON ty.oid=a.atttypid
        JOIN pg_catalog.pg_namespace tn ON tn.oid=ty.typnamespace
        JOIN pg_catalog.pg_collation co ON co.oid=a.attcollation
        JOIN pg_catalog.pg_namespace cn ON cn.oid=co.collnamespace
        WHERE n.nspname=%s AND t.relname=%s
          AND a.attnum>0 AND NOT a.attisdropped AND a.attname=ANY(%s)
          AND tn.nspname='pg_catalog' AND ty.typname IN ('varchar', 'bpchar')
          AND a.atttypmod>0
        ORDER BY a.attnum
    """, ('public', table_name, list(source_columns)))
    changes = cur.fetchall()
    if not changes:
        return []
    alterations = [sql.SQL('ALTER COLUMN {} TYPE pg_catalog.text COLLATE {}').format(
        sql.Identifier(column), sql.Identifier(collation_schema, collation_name))
        for column, _, collation_schema, collation_name in changes]
    cur.execute(sql.SQL('ALTER TABLE {} {}').format(
        sql.Identifier('public', table_name), sql.SQL(', ').join(alterations)))
    return [dict(column=column, from_type=old_type, to_type='text',
                 collation_schema=collation_schema, collation_name=collation_name)
            for column, old_type, collation_schema, collation_name in changes]


def load_dataset(fb, psycopg2, sql, admin, config, ddl, files, key_mode='strict', text_mode='strict'):
    require(key_mode in ('strict', 'surrogate'), 'SOURCE_KEY_MODE must be strict or surrogate')
    require(text_mode in ('strict', 'unbounded'), 'SOURCE_TEXT_MODE must be strict or unbounded')
    # admin remains connected to postgres and holds the per-database lock.
    with admin.cursor() as cur:
        cur.execute('SELECT pid FROM pg_stat_activity WHERE datname=%s', (config['dbname'],))
        require(not cur.fetchall(), 'Target DB has other connections; stop its other jobs first: ' + config['dbname'])
        cur.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(config['dbname'])))
        cur.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(config['dbname'])))
    records = []
    conn = psycopg2.connect(**config)
    try:
        conn.set_client_encoding('UTF8')
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                for file in files:
                    attrs = fb.columns(cur, 'public', file.stem)
                    names = [name for name in attrs if name not in ('__fact_id', 'provsql')]
                    require(names, 'No source columns for CSV: ' + str(file))
                    table = sql.Identifier('public', file.stem)
                    if '__fact_id' not in attrs:
                        cur.execute(sql.SQL('ALTER TABLE {} ADD COLUMN __fact_id BIGSERIAL').format(table))
                    key_audit = use_surrogate_key(cur, sql, file.stem) if key_mode == 'surrogate' else None
                    text_audit = widen_source_text(cur, sql, file.stem, names) if text_mode == 'unbounded' else []
                    command = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT CSV, DELIMITER ',')").format(
                        table, sql.SQL(',').join(map(sql.Identifier, names)))
                    before = file.stat()
                    with file.open('rb') as stream:
                        reader = HashedReader(stream)
                        try:
                            cur.copy_expert(command.as_string(conn), reader)
                        except Exception as exc:
                            if getattr(exc, 'pgcode', None) == '22001':
                                hint = ('Use SOURCE_TEXT_MODE=unbounded for generated strings that exceed '
                                        'built-in varchar(n)/char(n) source-column limits; values are not truncated.'
                                        if text_mode == 'strict' else
                                        'unbounded only widens built-in scalar varchar(n)/char(n) source columns; '
                                        'domains, arrays and other constraints keep their definitions.')
                                raise RuntimeError('String length limit while loading %s. %s\n%s' %
                                                   (file, hint, exc)) from exc
                            if getattr(exc, 'pgcode', None) == '23505':
                                hint = ('Generated CSVs whose business IDs are non-unique can use '
                                        'SOURCE_KEY_MODE=surrogate to retain every row and replace '
                                        'only source primary keys.' if key_mode == 'strict' else
                                        'Other UNIQUE constraints/indexes remain enforced in surrogate mode.')
                                raise RuntimeError('Duplicate key while loading %s. %s\n%s' %
                                                   (file, hint, exc)) from exc
                            raise
                    count = cur.rowcount
                    require(isinstance(count, int) and count >= 0, 'COPY did not report a row count: ' + str(file))
                    after = file.stat()
                    require(before.st_size == after.st_size == reader.byte_count and
                            before.st_mtime_ns == after.st_mtime_ns, 'CSV changed during import: ' + str(file))
                    records.append(dict(table='public.' + file.stem, csv=str(file),
                                        rows=count, bytes=reader.byte_count, sha256=reader.digest.hexdigest(),
                                        source_key_mode=key_mode, key_adaptation=key_audit,
                                        source_text_mode=text_mode, text_adaptations=text_audit))
                    print('Loaded: %s rows=%d' % (file.name, count), flush=True)
                cur.execute('ANALYZE')
        # Only return counts after the complete schema + COPY transaction commits.
        return records
    finally:
        conn.close()


def run_pricing(root, relative, size, folder):
    env = dict(os.environ, PYTHON=sys.executable, PYTHONUNBUFFERED='1',
               REBUILD_DB='0', SKIP_PREPARE='1')
    command = ['bash', str(root / 'scripts' / 'runPricingComparison.sh'),
               str(relative), size, os.environ['QUERIES']]
    with (folder / 'pricing.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace')
        try:
            for line in process.stdout:
                print(line, end='', flush=True)
                log.write(line)
                log.flush()
            return process.wait()
        except BaseException:
            # SIGINT lets the pricing runner clean up its own worker groups.
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            process.stdout.close()


def main():
    root = Path(sys.argv[1]).resolve()
    os.chdir(root)
    scenario_count = int(sys.argv[2])
    scenarios = sys.argv[3:3 + scenario_count]
    sizes = list(dict.fromkeys(sys.argv[3 + scenario_count:]))
    require(sizes, 'SIZES / DATA_SIZES cannot be empty')
    for size in sizes:
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', size), 'Invalid size name: ' + size)
    require(os.environ.get('SOURCE_SCHEMA', 'public') == 'public', 'Source schema must be public')
    key_mode = os.environ.get('SOURCE_KEY_MODE', 'strict')
    require(key_mode in ('strict', 'surrogate'), 'SOURCE_KEY_MODE must be strict or surrogate')
    text_mode = os.environ.get('SOURCE_TEXT_MODE', 'strict')
    require(text_mode in ('strict', 'unbounded'), 'SOURCE_TEXT_MODE must be strict or unbounded')
    for name, minimum in [('NUM_TESTS', 1), ('WARMUP_RUNS', 0)]:
        require(re.fullmatch(r'\d+', os.environ[name]) and int(os.environ[name]) >= minimum,
                '%s must be an integer >= %s' % (name, minimum))
    for name in ('REUSE_SQL', 'CONTINUE_ON_ERROR'):
        require(os.environ[name] in ('0', '1'), name + ' must be 0 or 1')
    require((root / 'scripts' / 'runPricingComparison.sh').is_file(),
            'Install this script in ForBackBench/scripts/ alongside runPricingComparison.sh')
    sys.path.insert(0, str(root / 'utilityTools'))
    from pricing.legacy_bridge import load_adapter
    from pricing.pricing_suite import parse_methods, preflight
    from pricing.price_generation import environment_settings
    price_config = environment_settings()
    print('Tuple prices: ' + json.dumps(price_config, sort_keys=True), flush=True)
    fb = load_adapter(root / 'utilityTools' / 'kcprice')
    psycopg2, sql = fb.dependencies()
    methods = parse_methods(os.environ['PRICING_METHODS'])
    preflight(methods, os.environ.get('C2D_BIN', str(root / 'utilityTools' / 'kcprice' / 'c2d')),
              smoke=True, kcprice_dir=root / 'utilityTools' / 'kcprice')

    # Validate ALL selected sizes before replacing the first database.
    jobs = []
    for scenario_spec in scenarios:
        _, scenario, relative = fb.get_paths(SimpleNamespace(root=root, scenario=scenario_spec, size=sizes[0]))
        config_file = scenario / 'postgres-config.ini'
        config = fb.read_pg_config(config_file)
        database = config['dbname']
        require(database not in ('postgres', 'template0', 'template1') and
                0 < len(database.encode('utf-8')) <= 63, 'Not an experimental database name: ' + database)
        schema_dir = scenario / 'schema' / 'oneToOne'
        ddl = (schema_dir / 's-schema.sql').read_text(encoding='utf-8-sig')
        require(ddl.strip(), 'Empty s-schema.sql: ' + str(schema_dir))
        source_schema = (schema_dir / 's-schema.txt').read_text(encoding='utf-8-sig')
        tables = set(re.findall(r'^\s*([^\s{}]+)\s*\{', source_schema, re.MULTILINE))
        require(tables, 'No source tables found in ' + str(schema_dir / 's-schema.txt'))
        ids = fb.query_ids(scenario, os.environ['QUERIES'])
        if os.environ['REUSE_SQL'] == '1':
            for query in ids:
                cached = root / 'experiments' / 'outputs' / 'gqr' / relative / 'rewritings' / ('Q' + query + '-SQL.txt')
                require(cached.is_file(), 'Missing cached SQL: %s; use REUSE_SQL=0 to generate it' % cached)
                fb.validate_sql(cached.read_text(encoding='utf-8'))
        datasets = []
        for size in sizes:
            files = fb.source_files(scenario, size)
            actual = {file.stem for file in files}
            require(actual == tables, '%s/%s CSV/schema mismatch: missing=%s extra=%s' %
                    (relative, size, sorted(tables - actual), sorted(actual - tables)))
            for file in files:
                with file.open('rb') as stream:
                    stream.read(1)
            datasets.append((size, files))
            print('Checked: %s / %s (%d CSV files)' % (relative, size, len(files)), flush=True)
        jobs.append((scenario, relative, config_file, config, ddl, datasets, ids))

    failed = False
    for scenario, relative, config_file, config, ddl, datasets, ids in jobs:
        admin = psycopg2.connect(**dict(config, dbname='postgres', application_name='ForBackBench-size-comparison'))
        try:
            admin.autocommit = True
            with admin.cursor() as cur:
                # Cooperative lock: two copies of THIS runner cannot interleave sizes.
                key = int.from_bytes(hashlib.sha256(('ForBackBench-sizes:' + config['dbname']).encode()).digest()[:8],
                                     byteorder='big', signed=True)
                cur.execute('SELECT pg_try_advisory_lock(%s)', (key,))
                require(cur.fetchone()[0], 'Another size-comparison runner is using DB ' + config['dbname'])
                cur.execute("SELECT 1 FROM pg_available_extensions WHERE name='provsql'")
                require(cur.fetchone(), 'ProvSQL is unavailable on the configured server')
            for size, files in datasets:
                folder = root / 'experiments' / 'pricing' / relative / size / 'loads' / stamp()
                folder.mkdir(parents=True, exist_ok=False)
                manifest = dict(scenario=str(relative), data_size=size, status='LOADING',
                                database=fb.public_config(config), methods=methods, queries=ids,
                                runs=int(os.environ['NUM_TESTS']), warmups=int(os.environ['WARMUP_RUNS']),
                                source_key_mode=key_mode,
                                source_text_mode=text_mode,
                                price_generation_config=price_config,
                                schema_sha256=hashlib.sha256(ddl.encode('utf-8')).hexdigest())
                save_manifest(folder, manifest)
                print('\n===== %s / %s =====' % (relative, size), flush=True)
                print('Rebuilding experimental DB %s at %s:%s' %
                      (config['dbname'], config['host'], config['port']), flush=True)
                print('Load audit: ' + str(folder / 'load_manifest.json'), flush=True)
                print('Source key mode: ' + key_mode, flush=True)
                print('Source text mode: ' + text_mode, flush=True)
                try:
                    require(fb.read_pg_config(config_file) == config, 'postgres-config.ini changed during this run')
                    started = time.perf_counter()
                    records = load_dataset(fb, psycopg2, sql, admin, config, ddl, files, key_mode, text_mode)
                    fingerprint = hashlib.sha256(json.dumps(
                        [(r['table'], r['sha256']) for r in records], separators=(',', ':')).encode()).hexdigest()
                    manifest.update(status='LOADED', tables=records, table_count=len(records),
                                    total_rows=sum(r['rows'] for r in records), dataset_sha256=fingerprint,
                                    load_seconds=time.perf_counter() - started)
                    save_manifest(folder, manifest)
                    print('Loaded %d rows; dataset SHA256=%s' % (manifest['total_rows'], fingerprint), flush=True)
                    manifest['prepare'] = fb.prepare(config, scenario, size, 'public', refresh_mappings=False)
                    require(fb.read_pg_config(config_file) == config, 'postgres-config.ini changed before pricing')
                    manifest['status'] = 'PRICING'
                    save_manifest(folder, manifest)
                    status = run_pricing(root, relative, size, folder)
                    manifest.update(status='OK' if status == 0 else 'PRICING_FAILED', pricing_exit_code=status)
                    save_manifest(folder, manifest)
                    if status:
                        failed = True
                        if status in (130, 143) or status < 0:
                            return 128 - status if status < 0 else status
                        if os.environ['CONTINUE_ON_ERROR'] != '1':
                            return 1
                except BaseException as exc:
                    manifest.update(status='INTERRUPTED' if isinstance(exc, KeyboardInterrupt) else 'ERROR', error=str(exc))
                    save_manifest(folder, manifest)
                    raise
        finally:
            admin.close()  # Releases the advisory lock even on failure / interrupt.
    return int(failed)


if __name__ == '__main__':
    def interrupt(signum, frame):
        raise KeyboardInterrupt('Stopped by signal %s' % signum)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('ERROR: interrupted; no next dataset will be loaded', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
PYTHON
