#!/usr/bin/env python3
"""ForBackBench -> GQR SQL -> ProvSQL -> joint KCPrice adapter.

The exact local user-modified benchmark scripts are not assumed. This adapter
can either price an existing raw GQR SQL file or run the published GQR command
line itself. Database rebuild is OFF unless explicitly confirmed.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import sys
import tempfile
import time
import uuid
from collections import deque
from contextlib import contextmanager
from itertools import zip_longest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kcprice import (KCError, DeadlineExceeded, Deadline, ProvenanceGraph,
                     VERSION, executable, run_process, sha256_file, solve, write_json)


def dependencies():
    try:
        import psycopg2
        from psycopg2 import sql
    except ImportError as exc:
        raise KCError("Install dependencies: python -m pip install -r utilityTools/kcprice/requirements.txt") from exc
    return psycopg2, sql


@contextmanager
def database_connection(config):
    psycopg2, _ = dependencies()
    conn = psycopg2.connect(**config)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def read_pg_config(path: Path) -> dict:
    """Read assignment-style INI; never source arbitrary shell code or log secrets."""
    recognized = {
        "PGHOST": "host", "PGPORT": "port", "PGUSER": "user",
        "PGPASSWORD": "password", "PGDATABASE": "dbname", "PGSSLMODE": "sslmode",
    }
    values = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise KCError("Invalid configuration line %d in %s" % (lineno, path))
        key, raw = line.split("=", 1)
        key = key.strip()
        if key not in recognized:
            continue
        raw = raw.strip()
        if raw.startswith(("'", '"')):
            parts = shlex.split(raw, comments=True)
            if len(parts) != 1:
                raise KCError("Use one quoted value for %s in %s" % (key, path))
            raw = parts[0]
        values[recognized[key]] = raw
    for key in ("host", "port", "user", "dbname"):
        if not values.get(key):
            raise KCError("Missing PostgreSQL %s in %s" % (key, path))
    try:
        values["port"] = int(values["port"])
    except ValueError as exc:
        raise KCError("Invalid PGPORT") from exc
    if not 1 <= values["port"] <= 65535:
        raise KCError("Invalid PGPORT")
    # Password may instead come from libpq's .pgpass. No default password.
    values["connect_timeout"] = 10
    values["application_name"] = "ForBackBench-KCPrice"
    return values


def public_config(config):
    return {k: v for k, v in config.items() if k != "password"}


def get_paths(args):
    root = Path(args.root).expanduser().resolve()
    candidate = Path(args.scenario).expanduser()
    scenario = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        relative = scenario.relative_to(root)
    except ValueError as exc:
        raise KCError("Scenario must be within the ForBackBench root directory") from exc
    if not scenario.is_dir():
        raise KCError("Scenario directory not found: %s" % scenario)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.size):
        raise KCError("Invalid data-size name")
    return root, scenario, relative


def source_files(scenario: Path, size: str):
    folder = scenario / "data" / "oneToOne" / size
    files = sorted(folder.glob("*.csv"))
    if not files:
        raise KCError("No source CSV files in %s; cannot establish the source-table whitelist" % folder)
    return files


def qname(schema: str, table: str, conn) -> str:
    _, sql = dependencies()
    return sql.Identifier(schema, table).as_string(conn)


def columns(cur, schema: str, table: str):
    cur.execute("""
        SELECT a.attname, a.atttypid::regtype::text
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
        JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY a.attnum
    """, (schema, table))
    return dict(cur.fetchall())


def _proc_exists(cur, signature: str) -> bool:
    cur.execute("SELECT to_regprocedure(%s)", (signature,))
    return cur.fetchone()[0] is not None


ADAPTER_VERSION = "navefr-compat-6"


def _routine_catalog(cur, name: str):
    """Read actual installed SQL signatures, not a guessed release number."""
    cur.execute("""SELECT p.oid, n.nspname, p.proname,
               ARRAY(SELECT t.typname FROM unnest(p.proargtypes) WITH ORDINALITY AS a(oid,pos)
                     JOIN pg_catalog.pg_type t ON t.oid=a.oid ORDER BY a.pos),
               p.proargnames, p.pronargdefaults,
               pg_get_function_identity_arguments(p.oid), p.proretset,
               rt.typname,
               ARRAY(SELECT tn.nspname
                     FROM unnest(p.proargtypes) WITH ORDINALITY AS a(oid,pos)
                     JOIN pg_catalog.pg_type t ON t.oid=a.oid
                     JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace ORDER BY a.pos),
               p.proargtypes::oid[]
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_catalog.pg_type rt ON rt.oid=p.prorettype
        WHERE p.proname=%s AND p.prokind='f'
          AND (n.nspname IN ('provsql','public') OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_depend d
                JOIN pg_catalog.pg_extension e ON e.oid=d.refobjid
                WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid
                  AND d.refclassid='pg_extension'::regclass AND d.deptype='e'
                  AND e.extname='provsql'))
        ORDER BY n.nspname, p.oid""", (name,))
    return [dict(oid=r[0], schema=r[1], name=r[2], types=list(r[3]),
                 names=list(r[4] or []), defaults=int(r[5]), identity=r[6],
                 returns_set=bool(r[7]), return_type=r[8],
                 type_schemas=list(r[9]), type_oids=list(r[10])) for r in cur.fetchall()]


def _quoted_identifier(name: str) -> str:
    """Quote a catalog-supplied SQL identifier (never an SQL type expression)."""
    return '"' + name.replace('"', '""') + '"'


def _signature_token_type(record) -> str:
    """Use the actual argument type namespace, not the function namespace."""
    token = record['types'][0]
    namespaces = record.get('type_schemas') or []
    if namespaces:
        return _quoted_identifier(namespaces[0]) + '.' + _quoted_identifier(token)
    # For offline historical reports only. Live catalog records always include
    # type_schemas; an unqualified type here follows the diagnostic search_path.
    if token not in ('uuid', 'provenance_token'):
        raise KCError('Unsupported evaluator token type: ' + token)
    return token


def _select_legacy_signature(routines):
    """Recognize both contracts actually reported by the user's PG12 database.

    (token, mapping, one, plus, times [,monus] [,delta])
    (token, mapping, one, value_type:regtype, plus, times [,monus] [,delta])

    token can be uuid or provenance_token. No claim is made that the latter is
    a UUID domain: its identity is preserved through its SQL text I/O. Optional
    operation roles are identified by name, never guessed from arity alone.
    """
    accepted = []
    for record in routines:
        types, names = record['types'], record['names']
        n = len(types)
        if record.get('returns_set') or n not in (5, 6, 7, 8):
            continue
        if types[0] not in ('uuid', 'provenance_token'):
            continue
        if types[1] not in ('regclass', 'text', 'varchar'):
            continue
        if types[2] not in ('anyelement', 'anycompatible', 'text', 'varchar'):
            continue
        if record.get('return_type', types[2]) not in ('anyelement','anycompatible','text','varchar'):
            continue
        explicit_value_type = types[3] == 'regtype'
        operator_offset = 4 if explicit_value_type else 3
        if n - operator_offset not in (2, 3, 4):
            continue
        if explicit_value_type and len(names) > 3 and names[3] and names[3].lower() not in (
                'value_type', 'valuetype', 'element_type', 'elementtype'):
            continue
        operator_types = ('regproc', 'regprocedure', 'oid', 'text', 'varchar')
        if any(t not in operator_types for t in types[operator_offset:]):
            continue
        operations = ['plus', 'times']
        valid = True
        for i, role in enumerate(operations, operator_offset):
            if i < len(names) and names[i] and role not in names[i].lower():
                valid = False
        for i in range(operator_offset + 2, n):
            argname = (names[i] or '').lower() if i < len(names) else ''
            if 'delta' in argname:
                op = 'delta'
            elif 'monus' in argname or 'minus' in argname:
                op = 'monus'
            else:
                valid = False
                break
            if op in operations:
                valid = False
                break
            operations.append(op)
        if valid:
            item = dict(record)
            item.update(operations=operations, operator_offset=operator_offset,
                        explicit_value_type=explicit_value_type,
                        token_sql_type=_signature_token_type(record))
            accepted.append(item)
    if not accepted:
        found = '; '.join('%s.%s(%s)' % (r['schema'], r['name'], r['identity'])
                          for r in routines) or '(none)'
        raise KCError('No supported provenance_evaluate signature. Installed signatures: ' + found +
                      '. Run checkProvSQL_KCPrice.sh and retain its JSON report; do not create fake gate APIs.')
    # Prefer the 7-argument polymorphic interface over its explicit-type
    # companion (8 args), when both have the same operator contract. Never use
    # database-specific OIDs as version numbers or overload-selection constants.
    score = lambda r: (r['schema'] == 'provsql', len(r['operations']), not r['explicit_value_type'])
    best = max(score(r) for r in accepted)
    accepted = [r for r in accepted if score(r) == best]
    if len(accepted) != 1:
        raise KCError('Ambiguous provenance_evaluate overloads; inspect the diagnostics report')
    return accepted[0]


def _type_description(cur, type_name: str):
    """Inspect a real PG type and its domain chain, without assuming an alias."""
    cur.execute("""WITH RECURSIVE chain AS (
          SELECT t.oid, n.nspname, t.typname, t.typtype, t.typbasetype, 0 AS depth
          FROM pg_catalog.pg_type t
          JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
          WHERE t.oid=to_regtype(%s)
          UNION ALL
          SELECT t.oid, n.nspname, t.typname, t.typtype, t.typbasetype, c.depth+1
          FROM chain c JOIN pg_catalog.pg_type t ON t.oid=c.typbasetype
          JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
          WHERE c.typtype='d' AND c.depth<16
        ) SELECT oid, nspname, typname, typtype, depth FROM chain ORDER BY depth""", (type_name,))
    return [dict(oid=r[0], schema=r[1], name=r[2], kind=r[3], depth=r[4]) for r in cur.fetchall()]


def _is_token_column_type(cur, type_name) -> bool:
    if not type_name:
        return False
    if type_name in ('uuid', 'pg_catalog.uuid', '"pg_catalog"."uuid"'):
        return True
    if type_name in ('text','varchar','character varying','bigint','integer','json','jsonb'):
        return False
    chain = _type_description(cur, type_name)
    if not chain:
        return False
    # A named provenance_token may be a domain or another SQL type. Actual
    # value conversion and semiring semantics are checked by --self-test.
    if chain[0]['name'] == 'provenance_token':
        return True
    return chain[0]['kind'] == 'd' and chain[-1]['schema'] == 'pg_catalog' and chain[-1]['name'] == 'uuid'


def _legacy_signature(cur):
    return _select_legacy_signature(_routine_catalog(cur, 'provenance_evaluate'))


def _provsql_backend(cur) -> str:
    """Select by capabilities. A fork name is NOT a promise of an SQL ABI."""
    requested = os.environ.get('PROVSQL_BACKEND', 'auto')
    if requested not in ('auto', 'gate-api', 'legacy-provenance-evaluate'):
        raise KCError('PROVSQL_BACKEND must be auto, gate-api or legacy-provenance-evaluate')
    if requested != 'legacy-provenance-evaluate':
        if _proc_exists(cur, 'provsql.get_gate_type(uuid)') and _proc_exists(cur, 'provsql.get_children(uuid)'):
            return 'gate-api'
        if requested == 'gate-api':
            raise KCError('gate-api was explicitly requested but get_gate_type/get_children are unavailable')
    _legacy_signature(cur)
    return 'legacy-provenance-evaluate'


def check_provsql(conn, schema: str):
    _, sql = dependencies()
    with conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname='provsql'")
        record = cur.fetchone()
        if not record:
            raise KCError("ProvSQL extension is absent in this database; run enableProvSQL_KCPrice.sh after installation")
        cur.execute(sql.SQL("SET search_path TO {}, provsql, public").format(sql.Identifier(schema)))
        # Resolve the installed helper through the configured search_path;
        # do not infer its schema solely from a repository name.
        if not _proc_exists(cur, "provenance()") and not _proc_exists(cur, "provsql.provenance()"):
            raise KCError("Installed ProvSQL lacks provenance()")
        backend = _provsql_backend(cur)
        cur.execute("SHOW shared_preload_libraries")
        preload = cur.fetchone()[0]
        # provsql.active is not present in all historical revisions.  Use a
        # savepoint so an unsupported GUC does not roll back CREATE EXTENSION
        # or any earlier setup in the transaction.
        cur.execute("SAVEPOINT kcprice_provsql_guc")
        try:
            cur.execute("SET provsql.active = on")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT kcprice_provsql_guc")
        finally:
            cur.execute("RELEASE SAVEPOINT kcprice_provsql_guc")
        cur.execute("SELECT version(), inet_server_port(), current_database()")
        server, port, database = cur.fetchone()
    return {"adapter_version": ADAPTER_VERSION,
            "postgres_version": server, "server_port": port, "database": database,
            "provsql_version": record[0], "provsql_backend": backend,
            "shared_preload_contains_provsql": "provsql" in (preload or "")}


def mapping_token_column(cur, schema: str, mapping: str) -> str:
    """Return the provenance-token column used by a mapping table.

    Accept UUID/provenance_token ``token`` or ``provenance`` columns based
    on the actual catalog, not a guessed fork version or UUID alias.
    No global persistent mapping is materialised.
    """
    attrs = columns(cur, schema, mapping)
    for name in ("provenance", "token"):
        if _is_token_column_type(cur, attrs.get(name)):
            return name
    raise KCError("%s has no supported provenance/token column; actual columns: %r" % (mapping, attrs))


def mapping_name(cur, schema: str, table: str) -> Optional[str]:
    # Earlier three-argument create_provenance_mapping calls may fold case.
    candidates = list(dict.fromkeys((table + "_tuple_mapping", (table + "_tuple_mapping").lower())))
    cur.execute("""SELECT c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relname=ANY(%s) AND c.relkind IN ('r','p')""",
                (schema, candidates))
    found = [row[0] for row in cur.fetchall()]
    if len(found) > 1:
        raise KCError("Ambiguous case-sensitive mappings for %s: %s" % (table, found))
    return found[0] if found else None





def _pricing_legacy_regclass_body_v2(function_name, body):
    """Return a narrowly checked repair; preserve every other function operation."""
    import re
    templates = {
        'add_provenance': """
BEGIN
  EXECUTE format('ALTER TABLE %I ADD COLUMN provsql provsql.provenance_token UNIQUE DEFAULT uuid_generate_v4()', _tbl);
  LOCK TABLE provenance_circuit_gate;
  EXECUTE format('INSERT INTO provsql.provenance_circuit_gate SELECT provsql, ''input'' FROM %I',_tbl);
  EXECUTE format('CREATE TRIGGER add_provenance_circuit_gate BEFORE INSERT ON %I FOR EACH ROW EXECUTE PROCEDURE provsql.add_provenance_circuit_gate_trigger()',_tbl);
END
""",
        'create_provenance_mapping': """
DECLARE
BEGIN
  EXECUTE format('CREATE TEMP TABLE tmp_provsql ON COMMIT DROP AS TABLE %I', oldtbl);
  ALTER TABLE tmp_provsql RENAME provsql TO provenance;
  EXECUTE format('CREATE TABLE %I AS SELECT %s AS value, provenance FROM tmp_provsql', newtbl, att);
  EXECUTE format('CREATE INDEX ON %I(provenance)', newtbl);
END
""",
    }
    if function_name not in templates:
        return body, 0
    literals = {
        'add_provenance': (
            'ALTER TABLE %I ADD COLUMN provsql provsql.provenance_token UNIQUE DEFAULT uuid_generate_v4()',
            "INSERT INTO provsql.provenance_circuit_gate SELECT provsql, ''input'' FROM %I",
            'CREATE TRIGGER add_provenance_circuit_gate BEFORE INSERT ON %I FOR EACH ROW EXECUTE PROCEDURE provsql.add_provenance_circuit_gate_trigger()',
        ),
        'create_provenance_mapping': (
            'CREATE TEMP TABLE tmp_provsql ON COMMIT DROP AS TABLE %I',
        ),
    }[function_name]
    def replace(text):
        for literal in literals:
            text = text.replace("'" + literal + "'", "'" + literal.replace('%I', '%s') + "'")
        return text
    def normalized(text):
        # Only standalone comment lines are ignored. SQL strings and all
        # executable statements must otherwise match the known implementation.
        text = re.sub(r'(?m)^[ \t]*--[^\r\n]*', '', text)
        text = re.sub(r'\s+', ' ', text).strip().rstrip(';')
        return re.sub(r'\s*,\s*', ',', text)
    changed = replace(body)
    count = sum(body.count("'" + literal + "'") for literal in literals)
    if not count:
        return body, 0  # Already fixed or a different/newer implementation.
    if normalized(changed) != normalized(replace(templates[function_name])):
        raise KCError('Unrecognized legacy body of provsql.%s; refusing to alter its initialization logic. '
                      'Inspect pg_get_functiondef before applying a version-specific repair.' % function_name)
    return changed, count


def _pricing_repair_legacy_regclass_v2(cur):
    """Run after CREATE EXTENSION on each DB, before source-table initialization.

    CREATE OR REPLACE retains the function OID, owner and grants. The server's
    own function definition preserves SECURITY DEFINER, SET search_path, and
    all other attributes. Both replacements run in the caller's transaction.
    """
    import hashlib
    cur.execute("""
        SELECT p.oid::pg_catalog.regprocedure::text, p.proname, l.lanname,
               p.proargnames, p.prosrc, pg_catalog.pg_get_functiondef(p.oid)
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_language l ON l.oid=p.prolang
        WHERE p.oid IN (
            pg_catalog.to_regprocedure('provsql.add_provenance(pg_catalog.regclass)'),
            pg_catalog.to_regprocedure('provsql.create_provenance_mapping(text,pg_catalog.regclass,text)')
        )
        ORDER BY p.proname
    """)
    planned = []
    expected_args = {'add_provenance': ['_tbl'],
                     'create_provenance_mapping': ['newtbl', 'oldtbl', 'att']}
    for signature, name, language, names, body, definition in cur.fetchall():
        if language != 'plpgsql':
            continue
        repaired, count = _pricing_legacy_regclass_body_v2(name, body)
        if not count:
            continue
        if list(names or []) != expected_args[name] or not body or definition.count(body) != 1:
            raise KCError('Unexpected function definition for %s; no replacement attempted' % signature)
        updated = definition.replace(body, repaired, 1)
        planned.append(dict(signature=signature, replacements=count,
                            original_definition=definition, patched_definition=updated,
                            before_sha256=hashlib.sha256(definition.encode()).hexdigest(),
                            after_sha256=hashlib.sha256(updated.encode()).hexdigest()))
    # Validate both definitions before replacing either one.
    for item in planned:
        cur.execute(item['patched_definition'])
        print('ProvSQL legacy regclass repair: %s (%d SQL statements)' %
              (item['signature'], item['replacements']), flush=True)
    return planned


def prepare(config: dict, scenario: Path, size: str, schema: str,
            refresh_mappings: bool = False) -> dict:
    """Only CSV-listed source tables are touched. No global fact mapping is made."""
    psycopg2, sql = dependencies()
    files = source_files(scenario, size)
    started = time.perf_counter()
    with database_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS provsql CASCADE")
            legacy_regclass_repairs = _pricing_repair_legacy_regclass_v2(cur)
        info = check_provsql(conn, schema)
        info["legacy_regclass_quote_repairs"] = legacy_regclass_repairs
        print("ProvSQL export backend: %s" % info["provsql_backend"], flush=True)
        conn.commit()
        for file in files:
            table = file.stem
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SET search_path TO {}, provsql, public").format(sql.Identifier(schema)))
                names = columns(cur, schema, table)
                if not names:
                    raise KCError("Source table %s.%s is absent; load its CSV first" % (schema, table))
                ident = sql.Identifier(schema, table)
                if "__fact_id" not in names:
                    cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN __fact_id BIGSERIAL").format(ident))
                # A tuple key is per relation, not globally unique across relations.
                index = "kcprice_id_" + hashlib.sha256((schema + "." + table).encode()).hexdigest()[:20]
                cur.execute(sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} (__fact_id)")
                            .format(sql.Identifier(index), ident))
                cur.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN __fact_id SET NOT NULL").format(ident))
                if "provsql" not in names:
                    # Let PostgreSQL resolve the installed management overload
                    # from a string relation name in the configured search_path.
                    cur.execute("SELECT add_provenance(%s)", (qname(schema, table, conn),))
                elif not _is_token_column_type(cur, names["provsql"]):
                    raise KCError("Source %s has an unsupported provsql column type: %s" % (table, names["provsql"]))
                existing = mapping_name(cur, schema, table)
                if existing and refresh_mappings:
                    cur.execute(sql.SQL("DROP TABLE {}") .format(sql.Identifier(schema, existing)))
                    existing = None
                if existing is None:
                    mapping = table + "_tuple_mapping"
                    if len(mapping.encode("utf-8")) > 63:
                        raise KCError("Mapping name exceeds PostgreSQL's 63-byte identifier limit: %s" % mapping)
                    # Default preserve_case=false accepts a safely quoted qualified
                    # identifier. This retains compatibility with the 3-arg API.
                    cur.execute("SELECT create_provenance_mapping(%s, %s, %s)",
                                (mapping, qname(schema, table, conn), "__fact_id"))
                    existing = mapping_name(cur, schema, table)
                    if existing is None:
                        raise KCError("ProvSQL did not create the expected tuple mapping for %s" % table)
                attrs = columns(cur, schema, existing)
                mapping_token_column(cur, schema, existing)
                if "value" not in attrs or "provsql" in attrs:
                    raise KCError("%s is not an ordinary tuple mapping. Refresh it explicitly." % existing)
            # Older mapping functions leave a fixed-name temporary table until
            # COMMIT; one transaction per source avoids that collision.
            conn.commit()
            print("ProvSQL: %s.%s -> %s" % (schema, table, existing), flush=True)
    info.update(prepare_seconds=time.perf_counter() - started, source_table_count=len(files))
    return info


def rebuild_database(config: dict, scenario: Path, size: str, schema: str, confirmation: str) -> None:
    """Destructive opt-in. Never forcibly terminate unrelated database sessions."""
    psycopg2, sql = dependencies()
    database = config["dbname"]
    if database in ("postgres", "template0", "template1") or confirmation != database:
        raise KCError("Database rebuild requires --confirm-rebuild equal to PGDATABASE; system databases are forbidden")
    if schema != "public":
        raise KCError("The supplied ForBackBench schema import is supported only in schema public")
    files = source_files(scenario, size)
    schema_file = scenario / "schema" / "oneToOne" / "s-schema.sql"
    if not schema_file.is_file():
        raise KCError("Missing source schema: %s" % schema_file)
    admin = dict(config, dbname="postgres")
    conn = psycopg2.connect(**admin)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_available_extensions WHERE name='provsql'")
            if not cur.fetchone():
                raise KCError("Install ProvSQL for this PostgreSQL server before rebuilding the benchmark database")
            print("WARNING: rebuilding ONLY database %s at %s:%s" % (database, config["host"], config["port"]), flush=True)
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    finally:
        conn.close()
    with database_connection(config) as conn:
        with conn.cursor() as cur:
            # This is the repository's trusted DDL file, not a query supplied by a remote user.
            cur.execute(schema_file.read_text(encoding="utf-8"))
            for file in files:
                table = file.stem
                attrs = columns(cur, schema, table)
                if not attrs:
                    raise KCError("CSV table not defined by the source schema: %s" % table)
                data_columns = [name for name in attrs if name not in ("__fact_id", "provsql")]
                # Assign IDs while loading, with an explicit COPY column list;
                # adding metadata columns cannot change CSV field counts.
                if "__fact_id" not in attrs:
                    cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN __fact_id BIGSERIAL")
                                .format(sql.Identifier(schema, table)))
                command = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT CSV, DELIMITER ',')").format(
                    sql.Identifier(schema, table), sql.SQL(",").join(map(sql.Identifier, data_columns)))
                with file.open("r", encoding="utf-8", newline="") as stream:
                    cur.copy_expert(command.as_string(conn), stream)
                print("Loaded: %s" % file.name, flush=True)
            cur.execute("ANALYZE")


def validate_sql(text: str) -> str:
    """Accept a single, explicit-projection positive SELECT/UNION query.

    This is a scope guard for GQR SQL, not a general SQL security sandbox.
    Counts, LIMITs, aggregates, negative subqueries, CTEs and wildcard source
    projections are intentionally rejected rather than assigned wrong prices.
    """
    try:
        import sqlparse
        from sqlparse import tokens as T
        from sqlparse.sql import Function, TokenList
    except ImportError as exc:
        raise KCError("Install sqlparse from utilityTools/kcprice/requirements.txt") from exc
    cleaned = sqlparse.format(text, strip_comments=True).strip()
    statements = [s.strip() for s in sqlparse.split(cleaned) if s.strip()]
    if len(statements) != 1:
        raise KCError("Expected exactly one raw GQR SELECT, not an empty file or several statements")
    query = statements[0].rstrip().removesuffix(";") if hasattr(str, "removesuffix") else statements[0].rstrip()
    if query.endswith(";"):
        query = query[:-1].rstrip()
    parsed = sqlparse.parse(query)[0]
    if parsed.get_type() != "SELECT":
        raise KCError("KCPrice expects a SELECT/UCQ, not DDL or DML")
    banned = {"LIMIT", "OFFSET", "FETCH", "EXCEPT", "INTERSECT", "WITH", "RECURSIVE",
              "HAVING", "GROUP BY", "INTO", "FOR", "WINDOW", "OVER", "DISTINCT ON",
              "LEFT JOIN", "RIGHT JOIN", "FULL JOIN", "LEFT OUTER JOIN", "RIGHT OUTER JOIN",
              "FULL OUTER JOIN", "NATURAL JOIN", "NOT", "NOT IN", "NOT EXISTS"}
    flat = [t for t in parsed.flatten() if not t.is_whitespace]
    for left, right in zip(flat, flat[1:]):
        if left.normalized == "DISTINCT" and right.normalized == "ON":
            raise KCError("DISTINCT ON can discard alternative witnesses; use ordinary DISTINCT")
    for token in flat:
        if token.ttype in T.Keyword and token.normalized in banned:
            raise KCError("SQL construct is outside positive UCQ pricing: %s" % token.normalized)
        if token.ttype in T.Keyword.DML and token.normalized != "SELECT":
            raise KCError("Only SELECT queries are allowed")
        if token.ttype in T.Wildcard:
            raise KCError("Use explicit original answer columns, not SELECT * or COUNT(*)")
        if (token.ttype in T.Name or token.ttype in T.String.Symbol) and token.value.strip('"').lower() in ("__fact_id", "provsql", "provenance"):
            raise KCError("Raw GQR SQL must not project provenance/tuple-ID metadata")
    todo = [parsed]
    while todo:
        token = todo.pop()
        if isinstance(token, Function) and str(token.get_name()).lower() != "cast":
            raise KCError("Functions/aggregates other than CAST are outside this GQR adapter: %s" % token.get_name())
        if isinstance(token, TokenList):
            todo.extend(token.tokens)
    if not query.strip():
        raise KCError("Empty SQL is an ERROR, not a zero-price query")
    return query


def check_query_sources(conn, query: str, schema: str, tables: List[str]):
    """Check plan base relations to prevent silently pricing untracked data as free."""
    allowed = {(schema, t) for t in tables}
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON, VERBOSE TRUE) " + query)
        plan = cur.fetchone()[0]
        if isinstance(plan, str):
            plan = json.loads(plan)
        todo = [plan]
        found = set()
        while todo:
            value = todo.pop()
            if isinstance(value, dict):
                if "Relation Name" in value:
                    found.add((value.get("Schema", schema), value["Relation Name"]))
                todo.extend(value.values())
            elif isinstance(value, list):
                todo.extend(value)
        if found - allowed:
            raise KCError("SQL references relations outside source CSV whitelist: %r" % sorted(found - allowed))
        for sch, table in sorted(found):
            if not _is_token_column_type(cur, columns(cur, sch, table).get("provsql")):
                raise KCError("Source table %s.%s has no provenance; do not price untracked tuples as free" % (sch, table))
    return sorted(found)


def _legacy_uuid_from_label(label: str) -> str:
    raw = label[1:]
    if not re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        raise KCError("Invalid legacy provenance variable: %s" % label)
    raw = raw.lower()
    return "%s-%s-%s-%s-%s" % (raw[:8], raw[8:12], raw[12:16], raw[16:20], raw[20:])


def _legacy_token_from_label(label: str) -> str:
    if label.startswith('x'):
        return _legacy_uuid_from_label(label)
    if not re.fullmatch(r'h(?:[0-9a-fA-F]{2})+', label):
        raise KCError('Malformed hex-encoded provenance token label')
    try:
        token = bytes.fromhex(label[1:]).decode('utf-8')
    except (ValueError, UnicodeError) as exc:
        raise KCError('Invalid UTF-8 provenance token label') from exc
    if not token or '\x00' in token:
        raise KCError('Empty/NUL token cannot identify a PostgreSQL provenance fact')
    return token


def _legacy_formula_graph(expressions, max_gates=0, max_chars=16777216, deadline=None):
    """Iterative parser for the expression semiring, with structural DAG sharing.

    No eval(), Python recursion, or left-deep flattening. The original SQL
    text form of each token is preserved across every formula. Resource guards FAIL, never truncate.
    """
    nodes, cache, roots = {}, {}, []
    total_chars = 0
    precedence = {'|': 1, '&': 2}
    lexer = re.compile(r"h[0-9a-fA-F]+|x[0-9a-fA-F]{32}|[01()&|]")
    # PostgreSQL text cannot contain NUL, so internal IDs cannot collide with
    # tokens even when provenance_token has non-UUID text input/output.
    internal = '\x00kcprice:'

    def checked_node(key, record):
        if key not in nodes:
            if max_gates and len(nodes) >= max_gates:
                raise KCError('Legacy provenance gate limit exceeded; no partial price is reported')
            nodes[key] = record
        return key

    def leaf(token):
        if token in ('0', '1'):
            key = internal + ('false' if token == '0' else 'true')
            return checked_node(key, {'type': 'zero' if token == '0' else 'one', 'children': []})
        key = _legacy_token_from_label(token)
        return checked_node(key, {'type': 'input', 'children': []})

    def combine(op, left, right):
        if left == right:
            return left
        identity = internal + ('true' if op == '&' else 'false')
        absorbing = internal + ('false' if op == '&' else 'true')
        if absorbing in (left, right):
            return absorbing
        if left == identity:
            return right
        if right == identity:
            return left
        key = (op, tuple(sorted((left, right))))
        if key not in cache:
            # A serial identifier avoids theoretical hash collisions; the dict
            # key itself establishes exact structural equality.
            node = internal + 'gate:%d' % len(cache)
            checked_node(node, {'type': 'times' if op == '&' else 'plus', 'children': list(key[1])})
            cache[key] = node
        return cache[key]

    for expression in expressions:
        if not isinstance(expression, str):
            raise KCError('Legacy evaluator must return a non-NULL text expression')
        total_chars += len(expression)
        if max_chars and total_chars > max_chars:
            raise KCError('Legacy formula text limit exceeded; no partial price is reported')
        values, operators = [], []
        pos, expecting_value, steps = 0, True, 0

        def reduce_operator():
            if len(values) < 2:
                raise KCError('Malformed legacy expression: missing operand')
            op = operators.pop()
            right, left = values.pop(), values.pop()
            values.append(combine(op, left, right))

        while pos < len(expression):
            if expression[pos].isspace():
                pos += 1
                continue
            match = lexer.match(expression, pos)
            if not match:
                raise KCError('Unsupported legacy expression near %r' % expression[pos:pos+80])
            tok, pos = match.group(), match.end()
            steps += 1
            if deadline is not None and steps % 1024 == 0:
                deadline.check()
            if expecting_value:
                if tok == '(':
                    operators.append(tok)
                elif tok in ('0','1') or tok.startswith(('x','h')):
                    values.append(leaf(tok))
                    expecting_value = False
                else:
                    raise KCError('Malformed legacy expression: expected an operand')
            elif tok in precedence:
                while operators and operators[-1] != '(' and precedence[operators[-1]] >= precedence[tok]:
                    reduce_operator()
                operators.append(tok)
                expecting_value = True
            elif tok == ')':
                while operators and operators[-1] != '(':
                    reduce_operator()
                if not operators:
                    raise KCError('Unbalanced legacy expression: unexpected closing parenthesis')
                operators.pop()
            else:
                raise KCError('Malformed legacy expression: missing binary operator')
        if expecting_value:
            raise KCError('Empty or truncated legacy provenance expression')
        while operators:
            if operators[-1] == '(':
                raise KCError('Unbalanced legacy expression: unclosed parenthesis')
            reduce_operator()
        if len(values) != 1:
            raise KCError('Malformed legacy provenance expression')
        roots.append(values[0])
        if deadline is not None:
            deadline.check()
    return ProvenanceGraph(nodes, roots), nodes


def _validate_legacy_mappings(conn, schema, tables, deadline, batch_size=1000):
    """Check exact live token/tuple-ID coverage before semiring evaluation.

    Some evaluators map an unnamed leaf to the identity. Without this check a
    stale snapshot mapping could make a priced fact disappear. Two sorted
    cursors provide an O(1)-buffer comparison without unsupported EXCEPT/NOT
    EXISTS operations on provenance-tracked source relations.
    """
    _, sql = dependencies()
    for table in tables:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('statement_timeout', %s, true)",
                        (str(max(1, int(deadline.remaining()*1000))),))
            mapping = mapping_name(cur, schema, table)
            if mapping is None:
                raise KCError('Missing *_tuple_mapping for %s.%s; run enableProvSQL_KCPrice.sh' % (schema,table))
            attrs = columns(cur, schema, mapping)
            tokcol = mapping_token_column(cur, schema, mapping)
            if 'value' not in attrs or 'provsql' in attrs:
                raise KCError('Mapping is malformed or provenance-tracked: %s' % mapping)
            if '__fact_id' not in columns(cur, schema, table):
                raise KCError('Source table %s lacks __fact_id; run enableProvSQL_KCPrice.sh' % table)
        with conn.cursor(name='kcprice_check_source') as src, conn.cursor(name='kcprice_check_mapping') as mapped:
            src.itersize = mapped.itersize = batch_size
            src.execute(sql.SQL('SELECT s.provsql::text, s.__fact_id::text FROM {} s ORDER BY s.provsql::text COLLATE "C"')
                        .format(sql.Identifier(schema, table)))
            mapped.execute(sql.SQL('SELECT m.{tok}::text, m.value::text FROM {tbl} m ORDER BY m.{tok}::text COLLATE "C"')
                           .format(tok=sql.Identifier(tokcol), tbl=sql.Identifier(schema, mapping)))
            previous = None
            for row_s, row_m in zip_longest(src, mapped):
                deadline.check()
                if row_s is None or row_m is None or tuple(row_s[:2]) != tuple(row_m[:2]):
                    raise KCError('Stale/inconsistent tuple mapping %s: live source token/ID pairs differ. '
                                  'Run enableProvSQL_KCPrice.sh with --refresh-mappings; no price was computed.' % mapping)
                token = row_s[0]
                if token is None or row_s[1] is None or token == previous:
                    raise KCError('NULL or duplicate source token in %s' % table)
                previous = token


def _legacy_mapping_view(cur, schema: str, tables: List[str], spec):
    """Materialize and index the typed mapping once per export.

    The historical helper name is retained for adapter compatibility. v5 used
    a UNION ALL view with token -> text -> token casts. Every recursive call
    of the old evaluator queried that view, including calls for internal
    gates absent from the input mapping. Those casts can prevent use of the
    source mappings' indexes. Store the final typed token and index it instead.

    Expose exactly (value, provenance), in that order. The former v3 view
    exposed (token, provenance, value): a positional/first-non-provenance
    reader can then use the raw token as the value. Do not add a token alias.
    Existing per-table tuple mappings are READ, never modified here.

    UUID spelling is not assumed for provenance_token. The evaluator receives
    tokens cast to its real first argument type. The expression parser decodes
    h<UTF8 hex> back to the original mapping token text without hash collisions.
    """
    _, sql = dependencies()
    arms = []
    for table in tables:
        mapping = mapping_name(cur, schema, table)
        if mapping is None:
            raise KCError("Missing per-table tuple mapping for %s.%s" % (schema, table))
        tokcol = mapping_token_column(cur, schema, mapping)
        arms.append(sql.SQL("SELECT m.{tok}::text AS token_text FROM {tbl} m")
                    .format(tok=sql.Identifier(tokcol), tbl=sql.Identifier(schema, mapping)))
    # This runs inside the rollback-only callback scope. Do not drop a caller's
    # object of the same name: collision is an error and scope rollback cleans
    # up our table. Existing source/mapping relations are only read.
    union = sql.SQL(" UNION ALL ").join(arms) if arms else sql.SQL("SELECT NULL::text AS token_text WHERE false")
    cur.execute(sql.SQL("""CREATE TEMP TABLE kcprice_eval_mapping ON COMMIT DROP AS
        SELECT ('h' || encode(convert_to(token_text, 'UTF8'), 'hex'))::text AS value,
               token_text::{tt} AS provenance
        FROM ({union}) kcprice_m""").format(tt=sql.SQL(spec['token_sql_type']), union=union))
    # Non-unique deliberately: the existing cross-mapping duplicate check
    # below must still report an invalid input catalog, not silently dedupe it.
    cur.execute("CREATE INDEX ON pg_temp.kcprice_eval_mapping (provenance)")
    cur.execute("ANALYZE pg_temp.kcprice_eval_mapping")


class LegacyCallbackError(KCError):
    """An old evaluator cannot safely resolve its callback names."""

    def __init__(self, message, details):
        super().__init__(message)
        self.legacy_callback_check = details


@contextmanager
def _legacy_callback_scope(cur):
    """Rollback-only subtransaction, including on successful export.

    PostgreSQL does not search pg_temp for functions. Old provenance_evaluate
    quotes a regproc's entire rendered name as one identifier. Register
    callbacks in an ordinary, private schema visible in a LOCAL search_path,
    not in pg_temp. Both the schema and the search_path change are undone at
    the savepoint, without rolling back the caller's earlier database work.
    """
    if cur.connection.autocommit:
        raise KCError('Legacy callback scope requires autocommit=False')
    nonce = uuid.uuid4().hex
    savepoint = 'kcprice_sp_' + nonce
    callback_schema = 'kcprice_cb_' + nonce
    cur.execute('SAVEPOINT ' + _quoted_identifier(savepoint))
    try:
        yield callback_schema
    finally:
        # Also recovers an aborted subtransaction after a PostgreSQL exception.
        # No DROP ... CASCADE, global search_path change, or extension update.
        cur.execute('ROLLBACK TO SAVEPOINT ' + _quoted_identifier(savepoint))
        cur.execute('RELEASE SAVEPOINT ' + _quoted_identifier(savepoint))


def _bind_legacy_callbacks(spec, callback_schema):
    match = re.fullmatch(r'kcprice_cb_([0-9a-f]{32})', callback_schema)
    if match is None:
        raise KCError('Invalid private callback schema identifier')
    result = dict(spec)
    result['callback_schema'] = callback_schema
    # Unique names also avoid regproc ambiguity with pre-existing overloads.
    result['callback_names'] = {
        op: 'kcprice_' + match.group(1) + '_' + op
        for op in ('concat','or_state','and_state','plus','times','monus','delta')
    }
    return result


def _legacy_callback_ref(spec, operation):
    # Before setup this is a diagnostic TEMPLATE only, not an existing schema.
    namespace = spec.get('callback_schema', 'kcprice_cb_runtime')
    name = spec.get('callback_names', {}).get(operation, 'kcprice_' + operation)
    if not re.fullmatch(r'kcprice_cb_(?:[0-9a-f]{32}|runtime)', namespace):
        raise KCError('Invalid callback namespace')
    if not re.fullmatch(r'kcprice_(?:[0-9a-f]{32}_)?[a-z_]+', name):
        raise KCError('Invalid callback identifier')
    return _quoted_identifier(namespace) + '.' + name


def _legacy_callback_ddl(spec):
    """Ordinary-schema functions/aggregates; caller owns a rollback savepoint."""
    template = r"""
        CREATE OR REPLACE FUNCTION pg_temp.kcprice_concat(a text, op text, b text)
        RETURNS text LANGUAGE plpgsql STABLE AS $$
        DECLARE cap bigint := current_setting('kcprice.legacy_max_chars')::bigint;
        BEGIN
          IF cap>0 AND octet_length(a)::bigint+octet_length(b)::bigint+3>cap THEN
            RAISE EXCEPTION 'KCPrice: legacy formula size guard exceeded (no partial price)'
              USING ERRCODE='54000';
          END IF;
          RETURN '(' || a || op || b || ')';
        END $$;
        CREATE OR REPLACE FUNCTION pg_temp.kcprice_or_state(state text, item text)
        RETURNS text LANGUAGE plpgsql STABLE AS $$
        BEGIN
          IF item IS NULL THEN
            RAISE EXCEPTION 'KCPrice: NULL child in legacy provenance plus';
          END IF;
          IF state IS NULL OR state='0' THEN RETURN item; END IF;
          IF item='0' THEN RETURN state; END IF;
          IF state='1' OR item='1' THEN RETURN '1'; END IF;
          RETURN pg_temp.kcprice_concat(state, '|', item);
        END $$;
        CREATE OR REPLACE FUNCTION pg_temp.kcprice_and_state(state text, item text)
        RETURNS text LANGUAGE plpgsql STABLE AS $$
        BEGIN
          IF item IS NULL THEN
            RAISE EXCEPTION 'KCPrice: NULL child in legacy provenance times';
          END IF;
          IF state IS NULL OR state='1' THEN RETURN item; END IF;
          IF item='1' THEN RETURN state; END IF;
          IF state='0' OR item='0' THEN RETURN '0'; END IF;
          RETURN pg_temp.kcprice_concat(state, '&', item);
        END $$;
        CREATE OR REPLACE FUNCTION pg_temp.kcprice_delta(item text)
        RETURNS text LANGUAGE SQL IMMUTABLE AS $$ SELECT item $$;
        CREATE OR REPLACE FUNCTION pg_temp.kcprice_monus(a text, b text)
        RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
        BEGIN
          RAISE EXCEPTION 'KCPrice legacy adapter supports positive UCQs only; monus was requested';
        END $$;
        DROP AGGREGATE IF EXISTS pg_temp.kcprice_plus(text);
        DROP AGGREGATE IF EXISTS pg_temp.kcprice_times(text);
        CREATE AGGREGATE pg_temp.kcprice_plus(text)
          (SFUNC=pg_temp.kcprice_or_state, STYPE=text, INITCOND='0');
        CREATE AGGREGATE pg_temp.kcprice_times(text)
          (SFUNC=pg_temp.kcprice_and_state, STYPE=text, INITCOND='1');
    """
    for op in ('concat','or_state','and_state','plus','times','monus','delta'):
        template = template.replace('pg_temp.kcprice_' + op,
                                    _legacy_callback_ref(spec, op))
    return template


def _verify_legacy_callbacks(cur, spec):
    """Exercise exactly the old quote_ident(regproc::text) name-resolution path.

    This is a real SQL callback check, not a substitute for the later ProvSQL
    OR/AND/self-join tests. Never silently accept a qualified regproc label.
    """
    details = {'status': 'ERROR', 'schema': spec['callback_schema'],
               'scope': 'ordinary schema inside rollback-only savepoint',
               'regproc_names': {}, 'probes': []}
    try:
        cur.execute("SELECT current_setting('search_path')")
        details['search_path'] = cur.fetchone()[0]
        for operation in ('plus','times','monus','delta'):
            ref = _legacy_callback_ref(spec, operation)
            cur.execute("SELECT %s::regproc::text, "
                        "pg_function_is_visible(%s::regproc::oid)", (ref, ref))
            row = cur.fetchone()
            rendered, visible = row if row is not None else (None, False)
            details['regproc_names'][operation] = rendered
            expected = spec['callback_names'][operation]
            if rendered != expected or not visible:
                raise KCError('Callback %s must render as the unqualified identifier %r; '
                              'got %r (visible=%r)' % (operation,expected,rendered,visible))
        # Quote the rendered regproc as ONE identifier, just like the SQL in
        # the user's failure. Ordering makes the small probe deterministic.
        for operation, operator, empty in (('plus','|','0'),('times','&','1')):
            fn = _quoted_identifier(details['regproc_names'][operation])
            probes = [
                ('two_inputs', 'SELECT ' + fn + "(v ORDER BY i) FROM "
                 "(VALUES (1,'h61'::text),(2,'h62'::text)) AS x(i,v)",
                 '(h61' + operator + 'h62)'),
                ('empty_input', 'SELECT ' + fn + "(v) FROM "
                 "(SELECT NULL::text AS v WHERE false) AS x", empty),
            ]
            for kind, statement, expected in probes:
                cur.execute(statement)
                value = cur.fetchone()[0]
                probe = {'operation': operation, 'case': kind,
                         'actual': value, 'expected': expected, 'passed': value == expected}
                details['probes'].append(probe)
                if value != expected:
                    raise KCError('Legacy %s callback %s failed' % (operation,kind))
        fn = _quoted_identifier(details['regproc_names']['delta'])
        cur.execute('SELECT ' + fn + "('h61'::text)")
        value = cur.fetchone()[0]
        details['probes'].append({'operation':'delta', 'case':'identity',
                                 'actual':value, 'expected':'h61', 'passed':value == 'h61'})
        if value != 'h61':
            raise KCError('Legacy delta callback must preserve Boolean support')
        details['status'] = 'PASSED'
        return details
    except Exception as exc:
        details['error'] = str(exc)
        error = LegacyCallbackError(str(exc), details)
        # Preserve timeout classification in price_one.
        error.pgcode = getattr(exc, 'pgcode', None)
        raise error from exc


def _legacy_semiring_setup(cur, schema: str, tables: List[str], callback_schema: str):
    """Must be called inside _legacy_callback_scope, never in autocommit."""
    spec = _bind_legacy_callbacks(_legacy_signature(cur), callback_schema)
    _legacy_mapping_view(cur, schema, tables, spec)
    callback_diagnostics = {'status':'ERROR', 'schema':callback_schema}
    try:
        cur.execute("SELECT has_database_privilege(current_database(), 'CREATE')")
        if not cur.fetchone()[0]:
            raise KCError('Legacy callbacks require CREATE privilege on this database '
                          'to create a rollback-only private schema; existing data is not changed')
        cur.execute('CREATE SCHEMA ' + _quoted_identifier(callback_schema))
        cur.execute('REVOKE ALL ON SCHEMA ' + _quoted_identifier(callback_schema) + ' FROM PUBLIC')
        cur.execute("SELECT current_setting('search_path')")
        previous_path = cur.fetchone()[0]
        new_path = 'pg_catalog, ' + _quoted_identifier(callback_schema) + ', ' + previous_path
        cur.execute("SELECT set_config('search_path', %s, true)", (new_path,))
        cur.execute(_legacy_callback_ddl(spec))
        spec['callback_check'] = _verify_legacy_callbacks(cur, spec)
    except LegacyCallbackError:
        raise
    except Exception as exc:
        callback_diagnostics['error'] = str(exc)
        error = LegacyCallbackError(str(exc), callback_diagnostics)
        error.pgcode = getattr(exc, 'pgcode', None)
        raise error from exc
    cur.execute("""SELECT provenance::text COLLATE "C" FROM pg_temp.kcprice_eval_mapping
                   GROUP BY provenance::text COLLATE "C" HAVING count(*)<>1 LIMIT 1""")
    if cur.fetchone() is not None:
        raise KCError('The same token appears in multiple tuple mappings; input facts must have unique tokens')
    spec['mapping_check'] = _verify_legacy_mapping(cur, spec)
    return spec


def _legacy_eval_statement(spec):
    """Build an explicit typed call from a validated pg_proc signature."""
    schema = '"' + spec['schema'].replace('"', '""') + '"'
    first = '%s::' + spec['token_sql_type']
    mapping = "'pg_temp.kcprice_eval_mapping'::" + spec['types'][1]
    value_type = spec['types'][2]
    identity = "'1'::" + ('text' if value_type in ('anyelement', 'anycompatible') else value_type)
    arguments = [first, mapping, identity]
    if spec['explicit_value_type']:
        # Keep this regtype consistent with the typed identity and callbacks.
        concrete_type = 'text' if value_type in ('anyelement', 'anycompatible') else value_type
        arguments.append("'%s'::regtype" % concrete_type)
    for operation, argument_type in zip(spec['operations'], spec['types'][spec['operator_offset']:]):
        fn = _legacy_callback_ref(spec, operation)
        if argument_type == 'regprocedure':
            fn += '(text,text)' if operation == 'monus' else '(text)'
            expression = "'%s'::regprocedure" % fn
        elif argument_type == 'oid':
            expression = "'%s'::regproc::oid" % fn
        else:
            expression = "'%s'::%s" % (fn, argument_type)
        arguments.append(expression)
    return 'SELECT %s.provenance_evaluate(%s)::text' % (schema, ', '.join(arguments))


def _legacy_eval_formula(cur, token: str, spec) -> str:
    cur.execute(_legacy_eval_statement(spec), (token,))
    value = cur.fetchone()[0]
    if value is None:
        raise KCError('legacy provenance_evaluate returned NULL for %s' % token)
    return str(value)



class LegacyMappingError(KCError):
    """Failure at the mapping -> evaluator boundary, with bounded diagnostics."""

    def __init__(self, message, details):
        super().__init__(message)
        self.legacy_mapping_check = details


def _legacy_label_from_token(token: str) -> str:
    if not isinstance(token, str) or not token or '\x00' in token:
        raise KCError('Expected a nonempty PostgreSQL token text without NUL')
    return 'h' + token.encode('utf-8').hex()


def _verify_legacy_mapping(cur, spec):
    """Read-only two-leaf round trip before exporting any answer formula.

    A valid signature alone cannot establish which mapping column an old
    evaluator reads. Assert the exact view descriptor and then check that
    evaluating real mapped inputs returns their supplied encoded leaf labels.
    Never accept a bare UUID as a replacement for this mapping contract.
    """
    details = {'status': 'ERROR', 'columns': [], 'samples': []}
    try:
        cur.execute('SELECT * FROM pg_temp.kcprice_eval_mapping LIMIT 0')
        descriptor = cur.description or []
        details['columns'] = [str(d[0]) for d in descriptor]
        if details['columns'] != ['value', 'provenance']:
            raise KCError('Temporary evaluator mapping must have exactly (value, provenance), in that order')
        cur.execute('SELECT provenance::text, value::text FROM pg_temp.kcprice_eval_mapping LIMIT 2')
        rows = cur.fetchall()
        for token, expected in rows:
            if token is None or expected is None:
                raise KCError('NULL token/value in temporary evaluator mapping')
            token, expected = str(token), str(expected)
            sample = {'token': token[:160], 'expected_label': expected[:256],
                      'actual_expression': None, 'passed': False}
            details['samples'].append(sample)
            if expected != _legacy_label_from_token(token):
                raise KCError('Temporary mapping value does not encode its own token')
            actual = _legacy_eval_formula(cur, token, spec)
            sample['actual_expression'] = actual[:256]
            sample['actual_length'] = len(actual)
            # Direct mapped inputs must return the assigned value. General
            # formula equivalence would conceal an incorrect mapping lookup.
            sample['passed'] = (actual == expected)
            if not sample['passed']:
                raise KCError('Mapped input did not round-trip through provenance_evaluate: '
                              'expected encoded label %r, received %r. No formula or price was accepted.'
                              % (expected[:120], actual[:120]))
        details['status'] = 'PASSED' if rows else 'NO_INPUTS'
        return details
    except Exception as exc:
        details['status'] = 'ERROR'
        details['error'] = str(exc)
        error = LegacyMappingError(str(exc), details)
        error.pgcode = getattr(exc, 'pgcode', None)
        raise error from exc

def fetch_provenance(conn, query: str, schema: str, tables: List[str],
                     seconds: float, batch_size: int, max_gates: int):
    started = time.perf_counter()
    deadline = Deadline(seconds)
    _, sql = dependencies()
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET search_path TO {}, provsql, public").format(sql.Identifier(schema)))
        cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, int(seconds * 1000))),))
        backend = _provsql_backend(cur)
    referenced = check_query_sources(conn, query, schema, tables)
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(deadline.remaining() * 1000))),))
    # DISTINCT happens BEFORE extracting row tokens: duplicate derivations of
    # one answer are ORed by ProvSQL rather than globally conjoined.
    wrapped = """SELECT provenance()::text AS __kc_root
FROM (SELECT DISTINCT * FROM (
%s
) AS __kc_raw) AS __kc_answers""" % query
    roots = []
    answer_count = 0
    with conn.cursor(name="kcprice_answers") as cur:
        cur.itersize = batch_size
        cur.execute(wrapped)
        for row in cur:
            deadline.check()
            token = row[0]
            if token is None:
                raise KCError("NULL answer provenance: check ProvSQL preload/source tracking")
            answer_count += 1
            roots.append(str(token))
    roots = sorted(set(roots))

    if backend == "legacy-provenance-evaluate":
        max_chars = int(os.environ.get('LEGACY_MAX_CHARS', '16777216'))
        if max_chars < 0:
            raise KCError('LEGACY_MAX_CHARS must be nonnegative; 0 disables the guard')
        # No formulas are required for a genuinely empty answer set.
        if not roots:
            graph, nodes = _legacy_formula_graph([])
            return graph, nodes, {"n_answers": 0, "provenance_seconds": time.perf_counter()-started,
                                  "provenance_sql": wrapped, "provsql_export_backend": backend,
                                  "legacy_formula_characters": 0}
        used_tables = [table for _, table in referenced]
        print("ProvSQL legacy: validating tuple mappings for %d source table(s)" % len(used_tables), flush=True)
        _validate_legacy_mappings(conn, schema, used_tables, deadline, batch_size)
        character_count = [0]
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('statement_timeout', %s, true)",
                        (str(max(1, int(deadline.remaining()*1000))),))
            cur.execute("SELECT set_config('kcprice.legacy_max_chars', %s, true)", (str(max_chars),))
            with _legacy_callback_scope(cur) as callback_schema:
                print("ProvSQL legacy: materializing and indexing evaluator mapping", flush=True)
                mapping_started = time.perf_counter()
                spec = _legacy_semiring_setup(cur, schema, used_tables, callback_schema)
                mapping_seconds = time.perf_counter() - mapping_started
                deadline.check()
                print("ProvSQL legacy: mapping ready in %.3fs; evaluating %d distinct answer token(s)" %
                      (mapping_seconds, len(roots)), flush=True)
                def expressions():
                    for root_index, root in enumerate(roots, 1):
                        cur.execute("SELECT set_config('statement_timeout', %s, true)",
                                    (str(max(1, int(deadline.remaining()*1000))),))
                        try:
                            text = _legacy_eval_formula(cur, root, spec)
                        except Exception as exc:
                            if getattr(exc, 'pgcode', None) == '57014':
                                raise DeadlineExceeded(
                                    "ProvSQL legacy expression evaluation canceled at answer token %d/%d "
                                    "(token=%s), after indexed mapping setup. The recursive evaluator "
                                    "may still expand a large formula. No partial price was accepted. "
                                    "Original database error: %s" % (root_index, len(roots), root, exc)) from exc
                            raise
                        character_count[0] += len(text)
                        if root_index == 1 or root_index % 100 == 0 or root_index == len(roots):
                            print("ProvSQL legacy: evaluated %d/%d answer token(s), %d formula characters" %
                                  (root_index, len(roots), character_count[0]), flush=True)
                        yield text
                graph, nodes = _legacy_formula_graph(expressions(), max_gates, max_chars, deadline)
        if not graph.satisfied(set(graph.inputs)):
            raise KCError("A returned answer's provenance is false even with every input tuple present")
        deadline.check()
        return graph, nodes, {"n_answers": answer_count,
                              "provenance_seconds": time.perf_counter()-started,
                              "provenance_sql": wrapped,
                              "provsql_export_backend": backend,
                              "legacy_evaluator_signature": spec['schema']+'.provenance_evaluate('+spec['identity']+')',
                              "legacy_eval_arity": len(spec['types']),
                              "legacy_token_sql_type": spec['token_sql_type'],
                              "legacy_explicit_value_type": spec['explicit_value_type'],
                              "legacy_token_encoding": "utf8-hex",
                              "legacy_mapping_check": spec['mapping_check'],
                              "legacy_mapping_storage": "indexed_temporary_table",
                              "legacy_mapping_setup_seconds": mapping_seconds,
                              "legacy_callback_check": spec['callback_check'],
                              "legacy_callback_cleanup": "rollback to savepoint completed",
                              "legacy_eval_sql": _legacy_eval_statement(spec),
                              "legacy_formula_characters": character_count[0],
                              "legacy_answer_tokens": roots}

    nodes = {}
    discovered = set(roots)
    queue = deque(roots)
    with conn.cursor() as cur:
        while queue:
            deadline.check()
            if max_gates and len(discovered) > max_gates:
                raise KCError("Provenance gate limit exceeded; no partial price is reported")
            batch = [queue.popleft() for _ in range(min(batch_size, len(queue)))]
            cur.execute("SELECT set_config('statement_timeout', %s, true)",
                        (str(max(1, int(deadline.remaining() * 1000))),))
            cur.execute("""SELECT t::text, provsql.get_gate_type(t)::text,
                         provsql.get_children(t)::text[]
                         FROM unnest(%s::uuid[]) AS u(t)""", (batch,))
            for token, kind, children in cur.fetchall():
                if kind is None:
                    raise KCError("Unknown provenance gate %s" % token)
                if any(c is None for c in (children or [])):
                    raise KCError("NULL child in provenance gate %s" % token)
                record = {"type": kind, "children": list(children or [])}
                nodes[token] = record
                for child in record["children"]:
                    if child not in discovered:
                        discovered.add(child)
                        queue.append(child)
            for token in batch:
                if nodes[token]["type"] == "assumed" and _proc_exists(cur, "provsql.get_extra(uuid)"):
                    cur.execute("SELECT provsql.get_extra(%s::uuid)", (token,))
                    extra = cur.fetchone()[0]
                    nodes[token]["extra"] = extra.lower() if extra else extra
    graph = ProvenanceGraph(nodes, roots)
    if not graph.satisfied(set(graph.inputs)):
        raise KCError("A returned answer's provenance is false even with every input tuple present")
    deadline.check()
    return graph, nodes, {"n_answers": answer_count, "provenance_seconds": time.perf_counter() - started,
                          "provenance_sql": wrapped, "provsql_export_backend": backend}

def resolve_tuples(conn, inputs: Tuple[str, ...], schema: str, tables: List[str],
                   seconds: float = 300):
    _, sql = dependencies()
    catalog = {}
    if not inputs:
        return catalog
    deadline = Deadline(seconds)
    # A temporary wanted-token relation avoids sending a huge token array once
    # for each base relation. It is not a persistent/global fact mapping.
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(deadline.remaining() * 1000))),))
        cur.execute('CREATE TEMP TABLE kcprice_wanted_tokens (provenance text COLLATE "C" PRIMARY KEY) ON COMMIT DROP')
        for offset in range(0, len(inputs), 10000):
            deadline.check()
            cur.execute("SELECT set_config('statement_timeout', %s, true)",
                        (str(max(1, int(deadline.remaining() * 1000))),))
            data = io.StringIO()
            csv.writer(data).writerows((token,) for token in inputs[offset:offset + 10000])
            data.seek(0)
            cur.copy_expert("COPY kcprice_wanted_tokens (provenance) FROM STDIN WITH (FORMAT CSV)", data)
        cur.execute("ANALYZE kcprice_wanted_tokens")
        seen_keys = {}
        for table in tables:
            deadline.check()
            cur.execute("SELECT set_config('statement_timeout', %s, true)",
                        (str(max(1, int(deadline.remaining() * 1000))),))
            mapping = mapping_name(cur, schema, table)
            if mapping is None:
                raise KCError("Missing per-table tuple mapping for %s.%s; run enableProvSQL_KCPrice.sh" % (schema, table))
            attrs = columns(cur, schema, mapping)
            tokcol = mapping_token_column(cur, schema, mapping)
            if "value" not in attrs or "provsql" in attrs:
                raise KCError("Invalid or provenance-tracked mapping: %s" % mapping)
            cur.execute(sql.SQL("""SELECT m.{tok}::text, m.value::text
                FROM {mapping} m JOIN pg_temp.kcprice_wanted_tokens w
                  ON m.{tok}::text COLLATE "C"=w.provenance""")
                        .format(tok=sql.Identifier(tokcol), mapping=sql.Identifier(schema, mapping)))
            for token, value in cur.fetchall():
                if value is None:
                    raise KCError("NULL tuple ID in mapping %s" % mapping)
                key = (schema + "." + table, value)
                if token in catalog:
                    raise KCError("A provenance token maps to multiple rows: %s" % token)
                if key in seen_keys and seen_keys[key] != token:
                    raise KCError("Duplicate tuple identifier in %s" % mapping)
                catalog[token] = {"relation": key[0], "tuple_id": value, "mapping": mapping}
                seen_keys[key] = token
    deadline.check()
    missing = set(inputs) - set(catalog)
    if missing:
        raise KCError("%d input tokens are missing from *_tuple_mapping (possibly stale maps); e.g. %s" %
                      (len(missing), next(iter(missing))))
    return catalog


def tuple_prices(catalog: dict, price_file: Optional[Path], unit_price: str,
                 random_seed: Optional[str], schema: str):
    if price_file and random_seed is not None:
        raise KCError("Choose either a price CSV or experimental random prices, not both")
    if price_file:
        price_file = price_file.expanduser().resolve()
        supplied = {}
        with price_file.open("r", newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if not {"relation", "tuple_id", "price"}.issubset(set(reader.fieldnames or [])):
                raise KCError("Price CSV header must include relation,tuple_id,price")
            for row in reader:
                relation = row["relation"].strip()
                if "." not in relation:
                    relation = schema + "." + relation
                key = relation, row["tuple_id"].strip()
                if key in supplied:
                    raise KCError("Duplicate price CSV key: %r" % (key,))
                supplied[key] = row["price"].strip()
        prices = {}
        for token, fact in catalog.items():
            key = fact["relation"], fact["tuple_id"]
            if key not in supplied:
                raise KCError("No seller price for %r; missing prices are never replaced by zero/unit price" % (key,))
            prices[token] = supplied[key]
        info = {"price_policy": "seller_csv", "price_file_sha256": sha256_file(price_file)}
    elif random_seed is not None:
        # Explicit experimental policy, NOT actual seller prices. Stable within
        # the same (relation, tuple ID) assignment; no use of unstable UUIDs.
        prices = {}
        for token, fact in catalog.items():
            key = json.dumps([random_seed, fact["relation"], fact["tuple_id"]], ensure_ascii=False)
            prices[token] = str(1 + int(hashlib.sha256(key.encode()).hexdigest(), 16) % 10)
        info = {"price_policy": "experimental_hash_integer_1_to_10", "price_seed": random_seed}
    else:
        prices = {token: unit_price for token in catalog}
        info = {"price_policy": "uniform_configured", "unit_price": unit_price}
    return prices, info


def extract_gqr_sql(log_text: str) -> str:
    if "rewNo:null" in log_text:
        raise KCError("GQR returned rewNo:null; this is not an empty database answer")
    # The published GQR tool prints SQL beginning with SELECT. Keep every line
    # from that point through the SQL terminator, rather than grep SELECT only.
    lines = log_text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^\s*(?:\(\s*)*SELECT\b", line, re.I)]
    if not starts:
        raise KCError("GQR output contains no SELECT SQL; inspect gqr.log")
    import sqlparse
    candidate = "\n".join(lines[starts[0]:])
    parts = [part for part in sqlparse.split(candidate) if part.strip()]
    if not parts or not parts[0].rstrip().endswith(";"):
        raise KCError("GQR SQL has no terminating semicolon; refusing to guess or drop UNION branches")
    if any(re.search(r"(?im)^\s*SELECT\b", part) for part in parts[1:]):
        raise KCError("GQR printed multiple SQL statements; supply one complete UCQ SQL explicitly")
    # Never recover from an invalid first SELECT by pricing a later UNION arm.
    return validate_sql(parts[0])


# Injected into the installed adapter by fix_gqr_input_compat.py.
# All names are private; KCError is supplied by that adapter.


def _gqr_compat_mask_v1(text, sql=False):
    chars = list(text)
    i = 0
    while i < len(text):
        if text[i] == "'" or (not sql and text[i] == '"'):
            quote, start = text[i], i
            escaped = (not sql or (i > 0 and text[i - 1] in 'eE'
                       and (i < 2 or not (text[i - 2].isalnum() or text[i - 2] == '_'))))
            i += 1
            while i < len(text):
                if text[i] == '\\' and escaped:
                    i += 2
                elif text[i] == quote:
                    if i + 1 < len(text) and text[i + 1] == quote:
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            else:
                raise KCError('GQR compatibility: unterminated quoted value')
            for j in range(start, min(i, len(text))):
                if chars[j] != '\n':
                    chars[j] = '_'
        elif ((not sql and (text.startswith('//', i) or text[i] == '#'))
              or (sql and text.startswith('--', i))):
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            chars[i:end] = ' ' * (end - i)
            i = end
        elif sql and text[i] == '"':
            # Preserve SQL identifiers while skipping over their contents.
            i += 1
            while i < len(text):
                if text[i] == '"':
                    if i + 1 < len(text) and text[i + 1] == '"':
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            else:
                raise KCError('GQR compatibility: unterminated SQL identifier')
        else:
            i += 1
    return ''.join(chars)


def _gqr_compat_atoms_v1(text):
    import re
    masked = _gqr_compat_mask_v1(text)
    pattern = re.compile('([A-Za-z_][A-Za-z_0-9-]*)\\s*\\(')
    records, group = [], []
    arrow, split = None, None
    pos = 0

    def finish():
        if not group or arrow is None or not split or split == len(group):
            raise KCError('GQR compatibility: expected a flat rule with one arrow')
        for j, record in enumerate(group):
            record['body'] = (j < split) if arrow == '->' else (j >= split)
        records.extend(group)

    for match in pattern.finditer(masked):
        if match.start() < pos:
            continue
        gap = masked[pos:match.start()].strip()
        if not group:
            if gap:
                raise KCError('GQR compatibility: unsupported rule prefix: ' + gap[:60])
        elif gap == '.':
            finish()
            group, arrow, split = [], None, None
        elif gap in ('->', '<-', ':-') and arrow is None:
            arrow, split = gap, len(group)
        elif gap != ',':
            raise KCError('GQR compatibility: unsupported rule separator: ' + gap[:60])
        end = masked.find(')', match.end())
        if end < 0 or '(' in masked[match.end():end]:
            raise KCError('GQR compatibility: nested or unclosed atom')
        args = masked[match.end():end].strip()
        if args and any(not value.strip() for value in args.split(',')):
            raise KCError('GQR compatibility: empty argument in atom')
        group.append(dict(name=match.group(1), arity=args.count(',') + 1 if args else 0,
                          start=match.start(1), end=match.end(1)))
        pos = end + 1
    if masked[pos:].strip() not in ('', '.'):
        raise KCError('GQR compatibility: unsupported trailing rule text')
    if group:
        finish()
    return records


def _gqr_compat_schema_v1(path):
    import re
    from pathlib import Path
    text = Path(path).read_text(encoding='utf-8-sig')
    masked = _gqr_compat_mask_v1(text)
    schemas, pos = {}, 0
    for match in re.finditer('([A-Za-z_][A-Za-z_0-9-]*)\\s*\\{([^{}]*)\\}', masked):
        if masked[pos:match.start()].strip():
            raise KCError('GQR compatibility: unsupported source schema text')
        name, fields = match.groups()
        if name in schemas:
            raise KCError('GQR compatibility: duplicate source schema: ' + name)
        columns = []
        for field in fields.split(','):
            part = re.fullmatch('\\s*([A-Za-z_][A-Za-z_0-9-]*)\\s*:\\s*[A-Za-z_][A-Za-z_0-9]*\\s*', field)
            if not part:
                raise KCError('GQR compatibility: unsupported schema column: ' + field[:80])
            columns.append(part.group(1))
        if len(set(columns)) != len(columns):
            raise KCError('GQR compatibility: duplicate source column: ' + name)
        schemas[name] = columns
        pos = match.end()
    if not schemas or masked[pos:].strip():
        raise KCError('GQR compatibility: empty/unsupported source schema: ' + str(path))
    return schemas


def _gqr_compat_inputs_v1(long_tgds, query_file, source_schema, folder):
    import hashlib
    import json
    import time
    from pathlib import Path
    started = time.perf_counter()
    files = [Path(long_tgds), Path(query_file)]
    raw = [p.read_bytes() for p in files]
    texts = [b.decode('utf-8-sig') for b in raw]
    scans = [_gqr_compat_atoms_v1(t) for t in texts]
    schemas = _gqr_compat_schema_v1(source_schema)
    signatures = {}
    for records in scans:
        for a in records:
            signatures.setdefault(a['name'], set()).add(a['arity'])
            if a['name'] in schemas and a['arity'] != len(schemas[a['name']]):
                raise KCError('GQR compatibility: source arity disagrees with schema: %s/%d' %
                              (a['name'], a['arity']))
    # SQL source relation names must remain the real database relation names.
    for atom in scans[0]:
        if atom['body'] and atom['name'] not in schemas:
            raise KCError('GQR compatibility: long-TGD body is not a source table: ' + atom['name'])
    conflicts = {n: arities for n, arities in signatures.items() if len(arities) > 1}
    mapping = {}
    for name, arities in sorted(conflicts.items()):
        if name in schemas or not name.endswith('Temp'):
            raise KCError('GQR compatibility: conflicting non-auxiliary predicate: ' + name)
        for arity in sorted(arities):
            new = name + '__gqr_arity_' + str(arity)
            if new in signatures:
                raise KCError('GQR compatibility: generated name already exists: ' + new)
            mapping[(name, arity)] = new
    normalized, occurrences = [], 0
    for text, records in zip(texts, scans):
        pieces, last = [], 0
        for atom in records:
            new = mapping.get((atom['name'], atom['arity']))
            if new:
                pieces.extend((text[last:atom['start']], new))
                last = atom['end']
                occurrences += 1
        pieces.append(text[last:])
        normalized.append(''.join(pieces))
    out = Path(folder) / 'gqr_input'
    out.mkdir(parents=True, exist_ok=True)
    rule_out, query_out = out / 'longTGDs.rule', out / Path(query_file).name
    for path, text in zip((rule_out, query_out), normalized):
        path.write_bytes(text.encode('utf-8'))
    details = dict(version='gqr-input-compat-2', predicate_names=len(conflicts),
                   signatures=len(mapping), renamed_occurrences=occurrences,
                   mapping=[dict(original=n, arity=a, renamed=new)
                            for (n, a), new in sorted(mapping.items())],
                   source_schema=str(source_schema),
                   source_schema_sha256=hashlib.sha256(Path(source_schema).read_bytes()).hexdigest(),
                   inputs=[dict(path=str(p), sha256=hashlib.sha256(b).hexdigest(),
                                normalized_sha256=hashlib.sha256(t.encode('utf-8')).hexdigest())
                           for p, b, t in zip(files, raw, normalized)])
    manifest = out / 'compatibility.json'
    manifest.write_text(json.dumps(details, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('GQR input compatibility: %d auxiliary names -> %d signatures; %s' %
          (len(conflicts), len(mapping), manifest), flush=True)
    return rule_out, query_out, dict(gqr_input_compatibility=details,
                                   gqr_input_compatibility_file=str(manifest),
                                   gqr_input_compatibility_seconds=time.perf_counter() - started)


def _gqr_compat_bind_sql_v1(query, source_schema, folder):
    """Bind GQR's per-table positional cN names to the original source columns.

    Parse each flat UNION branch independently. Never replace text inside SQL
    constants or conflate aliases reused for different tables across branches.
    """
    import hashlib
    import json
    import re
    import time
    from pathlib import Path
    started = time.perf_counter()
    schemas = _gqr_compat_schema_v1(source_schema)
    masked = _gqr_compat_mask_v1(query, sql=True)
    token_pattern = re.compile(r'"(?:[^"]|"")*"|[A-Za-z_][A-Za-z_0-9]*|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|\S')
    def name(token):
        return token[1:-1].replace('""', '"') if token.startswith('"') else token.lower()
    def keyword(token, word):
        return not token.startswith('"') and token.upper() == word
    edits, tables, branches, count = [], set(), 0, 0

    def bind(tokens):
        nonlocal branches, count
        if not tokens or not keyword(tokens[0].group(), 'SELECT'):
            raise KCError('GQR compatibility: expected a flat SELECT branch')
        if any(keyword(t.group(), 'SELECT') for t in tokens[1:]):
            raise KCError('GQR compatibility: nested SELECT requires explicit column binding')
        from_indices = [i for i, t in enumerate(tokens) if keyword(t.group(), 'FROM')]
        if len(from_indices) != 1:
            raise KCError('GQR compatibility: expected one FROM per UNION branch')
        i, aliases = from_indices[0] + 1, {}
        while i < len(tokens):
            if keyword(tokens[i].group(), 'WHERE') or tokens[i].group() == ';':
                break
            if i + 2 >= len(tokens) or not keyword(tokens[i + 1].group(), 'AS'):
                raise KCError('GQR compatibility: unsupported GQR source alias syntax')
            table = name(tokens[i].group())
            alias = name(tokens[i + 2].group())
            if table not in schemas or alias in aliases:
                raise KCError('GQR compatibility: unknown source or duplicate alias: ' + table)
            aliases[alias] = table
            tables.add(table)
            i += 3
            if i >= len(tokens) or tokens[i].group() != ',':
                if i < len(tokens) and not keyword(tokens[i].group(), 'WHERE') and tokens[i].group() != ';':
                    raise KCError('GQR compatibility: unsupported GQR FROM clause')
                break
            i += 1
        if not aliases:
            raise KCError('GQR compatibility: no source aliases')
        for left, dot, right in zip(tokens, tokens[1:], tokens[2:]):
            if dot.group() != '.':
                continue
            alias = name(left.group())
            if alias not in aliases:
                raise KCError('GQR compatibility: unknown column qualifier: ' + left.group())
            ordinal = re.fullmatch(r'c([0-9]+)', name(right.group()))
            if not ordinal:
                raise KCError('GQR compatibility: expected an inferred cN column: ' + right.group())
            columns = schemas[aliases[alias]]
            index = int(ordinal.group(1))
            if index >= len(columns):
                raise KCError('GQR compatibility: column index out of source range: ' + aliases[alias])
            replacement = '"' + columns[index].replace('"', '""') + '"'
            edits.append((right.start(), right.end(), replacement))
            count += 1
        branches += 1

    branch = []
    for token in token_pattern.finditer(masked):
        if keyword(token.group(), 'UNION'):
            bind(branch)
            branch = []
        elif not branch and keyword(token.group(), 'ALL'):
            raise KCError('GQR compatibility: unexpected UNION ALL')
        else:
            branch.append(token)
    bind(branch)
    pieces, last = [], 0
    for start, end, replacement in edits:
        pieces.extend((query[last:start], replacement))
        last = end
    pieces.append(query[last:])
    bound = ''.join(pieces)
    out = Path(folder) / 'gqr_input'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'query_inferred_columns.sql').write_bytes((query.rstrip(';') + ';\n').encode('utf-8'))
    details = dict(version='gqr-source-columns-1', union_branches=branches,
                   references=count, source_schema=str(source_schema),
                   source_schema_sha256=hashlib.sha256(Path(source_schema).read_bytes()).hexdigest(),
                   columns={t: schemas[t] for t in sorted(tables)},
                   inferred_sql_sha256=hashlib.sha256(query.encode('utf-8')).hexdigest(),
                   bound_sql_sha256=hashlib.sha256(bound.encode('utf-8')).hexdigest())
    (out / 'column_binding.json').write_text(json.dumps(details, indent=2) + '\n', encoding='utf-8')
    print('GQR source columns: %d references bound across %d UNION branches' % (count, branches), flush=True)
    return bound, dict(gqr_column_binding=details, gqr_column_binding_seconds=time.perf_counter() - started)


def obtain_gqr_sql(root: Path, scenario: Path, relative: Path, query_id: str,
                   folder: Path, args) -> Tuple[Path, dict]:
    saved = root / "experiments" / "outputs" / "gqr" / relative / "rewritings" / ("Q%s-SQL.txt" % query_id)
    info = {}
    if args.reuse_sql:
        if not saved.is_file() or not saved.stat().st_size:
            raise KCError("Cannot reuse missing/empty GQR SQL: %s" % saved)
        query = validate_sql(saved.read_text(encoding="utf-8"))
        info.update(rewrite_mode="explicitly_reused_sql", gqr_seconds=0.0, tgd_seconds=0.0)
    else:
        java = executable(args.java)
        gqr_jar = root / "systems" / "GQR" / "GQR.jar"
        query_file = scenario / "queries" / "Chasebench" / ("Q" + query_id) / ("Q" + query_id + ".txt")
        long_tgds = scenario / "dependencies" / "longTGDs.rule"
        for needed in (gqr_jar, query_file):
            if not needed.is_file():
                raise KCError("Missing GQR input: %s" % needed)
        t = time.perf_counter()
        if args.regenerate_tgds or not long_tgds.is_file() or not long_tgds.stat().st_size:
            utility = root / "utilityTools" / "RulewerkLongTGDs.jar"
            st = scenario / "dependencies" / "oneToOne-st-tgds.txt"
            tt = scenario / "dependencies" / "oneToOne-t-tgds.txt"
            for needed in (utility, st, tt):
                if not needed.is_file():
                    raise KCError("Missing long-TGD input: %s" % needed)
            fd, name = tempfile.mkstemp(prefix="kcprice-long-", suffix=".rule", dir=str(long_tgds.parent))
            os.close(fd)
            temp = Path(name)
            try:
                print("Generating long TGDs; see %s" % (folder / "long_tgds.log"), flush=True)
                run_process([java, "-jar", str(utility), "-st-tgds", str(st), "-t-tgds", str(tt),
                             "-out", str(temp)], folder / "long_tgds.log", args.gqr_timeout, cwd=root / "scripts")
                if temp.stat().st_size == 0:
                    raise KCError("Long-TGD generator produced an empty file; inspect its native-library errors")
                os.replace(str(temp), str(long_tgds))
            finally:
                if temp.exists():
                    temp.unlink()
            info["tgd_mode"] = "generated"
        else:
            info["tgd_mode"] = "existing_nonempty_file"
        info["tgd_seconds"] = time.perf_counter() - t
        info["long_tgds_sha256"] = sha256_file(long_tgds)
        gqr_rules, gqr_query, gqr_compat_info = _gqr_compat_inputs_v1(long_tgds, query_file, scenario / 'schema' / 'oneToOne' / 's-schema.txt', folder)
        info.update(gqr_compat_info)
        t = time.perf_counter()
        print("GQR rewriting Q%s; see %s" % (query_id, folder / "gqr.log"), flush=True)
        run_process([java, "-jar", str(gqr_jar), "-st-tgds", str(gqr_rules), "-q", str(gqr_query)],
                    folder / "gqr.log", args.gqr_timeout, cwd=root / "scripts")
        info["gqr_seconds"] = time.perf_counter() - t
        info.update(rewrite_mode="fresh_gqr", gqr_jar_sha256=sha256_file(gqr_jar),
                    query_file_sha256=sha256_file(query_file))
        query = extract_gqr_sql((folder / "gqr.log").read_text(encoding="utf-8", errors="replace"))
        query, gqr_columns_info = _gqr_compat_bind_sql_v1(query, scenario / 'schema' / 'oneToOne' / 's-schema.txt', folder)
        info.update(gqr_columns_info)
        saved.parent.mkdir(parents=True, exist_ok=True)
        temporary = saved.with_name(saved.name + ".kcprice.tmp")
        temporary.write_text(query + ";\n", encoding="utf-8")
        os.replace(str(temporary), str(saved))
    sql_file = folder / "query.sql"
    sql_file.write_text(query + ";\n", encoding="utf-8")
    return sql_file, info


SUMMARY_FIELDS = ["batch_id", "query", "run", "status", "price", "n_answers", "n_tuples",
                  "selected_count", "n_provenance_gates", "n_cnf_vars", "n_cnf_clauses",
                  "n_nnf_nodes", "n_nnf_edges", "provenance_seconds", "mapping_seconds",
                  "cnf_seconds", "compile_seconds", "nnf_read_seconds", "price_seconds",
                  "verify_seconds", "kc_total_seconds", "query_pipeline_seconds", "price_policy",
                  "db_mode", "error", "result_directory"]


def append_summary(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", newline="", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0, os.SEEK_END)
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        if stream.tell() == 0:
            writer.writeheader()
        writer.writerow(record)
        stream.flush()
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def price_one(config: dict, scenario: Path, query_file: Path, out: Path, args,
              metadata: dict) -> dict:
    psycopg2, _ = dependencies()
    started = time.perf_counter()
    query = validate_sql(query_file.read_text(encoding="utf-8"))
    metadata = dict(metadata, sql_sha256=sha256_file(query_file), db_config=public_config(config))
    catalog = {}
    try:
        tables = [file.stem for file in source_files(scenario, args.size)]
        conn = psycopg2.connect(**config)
        try:
            # All answer tokens and mapping reads refer to ONE database snapshot.
            conn.set_session(isolation_level="REPEATABLE READ")
            metadata.update(check_provsql(conn, args.schema))
            graph, raw_nodes, prov_info = fetch_provenance(conn, query, args.schema, tables,
                                                        args.provenance_timeout, args.batch_size, args.max_gates)
            wrapped = prov_info.pop("provenance_sql")
            (out / "provenance_query.sql").write_text(wrapped + ";\n", encoding="utf-8")
            metadata.update(prov_info)
            t = time.perf_counter()
            catalog = resolve_tuples(conn, graph.inputs, args.schema, tables,
                                     args.provenance_timeout)
            metadata["mapping_seconds"] = time.perf_counter() - t
            # Tokens now refer to the exported in-memory circuit; no live DB
            # connection is needed during potentially long c2d compilation.
            conn.rollback()
        finally:
            conn.close()
        prices, policy = tuple_prices(catalog, args.prices_csv, args.unit_price, args.price_seed, args.schema)
        metadata.update(policy)
        write_json(out / "provenance.json", {"roots": list(graph.roots), "nodes": raw_nodes, "prices": prices})
        with (out / "tuple_catalog.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["provenance", "relation", "tuple_id", "price", "mapping"])
            for token in graph.inputs:
                fact = catalog[token]
                writer.writerow([token, fact["relation"], fact["tuple_id"], prices[token], fact["mapping"]])
        result = solve(graph, prices, out, args.c2d, args.timeout,
                       not args.no_prune, args.check_decomposable, metadata)
        if result["status"] == "UNSAT":
            raise KCError("Joint lineage of observed answers is UNSAT: inspect provenance/c2d output, not a zero price")
        with (out / "selected_tokens.csv").open("r", newline="", encoding="utf-8") as source, \
             (out / "selected_tuples.csv").open("w", newline="", encoding="utf-8") as dest:
            writer = csv.writer(dest)
            writer.writerow(["relation", "tuple_id", "provenance", "price"])
            for row in csv.DictReader(source):
                fact = catalog[row["provenance"]]
                writer.writerow([fact["relation"], fact["tuple_id"], row["provenance"], row["price"]])
    except Exception as exc:
        previous = out / "result.json"
        result = json.loads(previous.read_text()) if previous.exists() else dict(metadata)
        timeout = isinstance(exc, DeadlineExceeded) or getattr(exc, "pgcode", None) == "57014"
        result.update(algorithm="KCPrice", status="TIMEOUT" if timeout else "ERROR", error=str(exc), price=None)
    result["query_pipeline_seconds"] = time.perf_counter() - started
    result["result_directory"] = str(out)
    write_json(out / "result.json", result)
    return result


def query_ids(scenario: Path, spec: str):
    if spec.lower() == "all":
        folders = (scenario / "queries" / "Chasebench").glob("Q*")
        ids = [p.name[1:] for p in folders if p.is_dir() and re.fullmatch(r"Q\d+", p.name)]
        ids.sort(key=int)
    else:
        ids = [x.strip().lstrip("Qq") for x in spec.split(",")]
    if not ids or any(not re.fullmatch(r"\d+", x) for x in ids):
        raise KCError("Queries must be 'all' or comma-separated integer IDs, e.g. 1,2,3")
    return list(dict.fromkeys(ids))


def diagnose(config, scenario, size, schema, self_test=False, seconds=300, batch_size=1000):
    """Inspect the installed API and optionally test real OR/AND propagation.

    Never installs/upgrades the extension, changes source rows, or runs c2d.
    The optional semantic test reads two real source rows and creates only
    rollback-only helper objects in a private ordinary schema and pg_temp;
    ProvSQL may record additional query gates.
    """
    psycopg2, sql = dependencies()
    report = {'adapter_version': ADAPTER_VERSION, 'config': public_config(config),
              'requested_backend': os.environ.get('PROVSQL_BACKEND', 'auto'),
              'status': 'ERROR', 'live_semantics_verified': False}
    conn = psycopg2.connect(**config)
    try:
        conn.set_session(isolation_level='REPEATABLE READ')
        with conn.cursor() as cur:
            cur.execute(sql.SQL('SET search_path TO {}, provsql, public').format(sql.Identifier(schema)))
            cur.execute('SELECT extversion FROM pg_extension WHERE extname=%s', ('provsql',))
            extension = cur.fetchone()
            report['provsql_version'] = extension[0] if extension else None
            report['legacy_evaluators'] = _routine_catalog(cur, 'provenance_evaluate')
            report['gate_api_available'] = (
                _proc_exists(cur, 'provsql.get_gate_type(uuid)') and
                _proc_exists(cur, 'provsql.get_children(uuid)'))
        report.update(check_provsql(conn, schema))
        if report['provsql_backend'] == 'legacy-provenance-evaluate':
            spec = _select_legacy_signature(report['legacy_evaluators'])
            report['selected_legacy_evaluator'] = spec
            with conn.cursor() as cur:
                report['legacy_token_type_info'] = _type_description(cur, spec['token_sql_type'])
            report['legacy_eval_sql_template'] = _legacy_eval_statement(spec)
            report['legacy_token_encoding'] = 'utf8-hex'
            report['legacy_mapping_columns_expected'] = ['value', 'provenance']
            report['legacy_mapping_storage'] = 'indexed_temporary_table'
            report['legacy_callback_strategy'] = 'private ordinary schema + LOCAL search_path + rollback savepoint'
            report['legacy_eval_sql_is_template'] = True
        report['status'] = 'API_OK'
        if not self_test:
            report['note'] = 'SQL signature compatibility only; use --self-test for real provenance semantics'
            return report
        tables = [p.stem for p in source_files(scenario, size)]
        sample = None
        with conn.cursor() as cur:
            for table in tables:
                cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1,int(seconds*1000))),))
                attrs = columns(cur, schema, table)
                if not _is_token_column_type(cur, attrs.get('provsql')) or '__fact_id' not in attrs:
                    continue
                cur.execute(sql.SQL('SELECT s.provsql::text, s.__fact_id FROM {} s '
                                    'ORDER BY s.__fact_id LIMIT 2').format(sql.Identifier(schema, table)))
                rows = cur.fetchall()
                if len(rows) == 2:
                    sample = table, rows[0][:2], rows[1][:2]
                    break
        if sample is None:
            raise KCError('Semantic self-test needs one tracked source table with __fact_id and at least two rows; '
                          'run enableProvSQL_KCPrice.sh first')
        table, (ta, ida), (tb, idb) = sample
        ident = sql.Identifier(schema, table).as_string(conn)
        va, vb = sql.Literal(ida).as_string(conn), sql.Literal(idb).as_string(conn)
        tests = [
            ('input', 'SELECT 1 AS answer FROM %s WHERE __fact_id=%s' % (ident,va),
             [False,True,False,True], {ta}),
            ('or_and_distinct', 'SELECT 1 AS answer FROM %s WHERE __fact_id IN (%s,%s)' % (ident,va,vb),
             [False,True,True,True], {ta,tb}),
            ('and_join', 'SELECT 1 AS answer FROM %s a CROSS JOIN %s b '
             'WHERE a.__fact_id=%s AND b.__fact_id=%s' % (ident,ident,va,vb),
             [False,False,False,True], {ta,tb}),
            ('self_join_same_fact', 'SELECT 1 AS answer FROM %s a CROSS JOIN %s b '
             'WHERE a.__fact_id=%s AND b.__fact_id=%s' % (ident,ident,va,va),
             [False,True,False,True], {ta}),
        ]
        report['self_tests'] = []
        for name, query, expected, expected_inputs in tests:
            report['self_test_current'] = name
            try:
                graph, nodes, details = fetch_provenance(conn, query, schema, [table], seconds, batch_size, 10000)
            except Exception as exc:
                report['self_tests'].append({'name': name, 'passed': False, 'error': str(exc)})
                raise
            if 'legacy_mapping_check' in details:
                report['legacy_mapping_check'] = details['legacy_mapping_check']
                report['legacy_mapping_storage'] = details['legacy_mapping_storage']
                report['legacy_mapping_setup_seconds'] = details['legacy_mapping_setup_seconds']
            if 'legacy_callback_check' in details:
                report['legacy_callback_check'] = details['legacy_callback_check']
                report['legacy_callback_cleanup'] = details['legacy_callback_cleanup']
                report['legacy_eval_sql_template'] = details['legacy_eval_sql']
                report['legacy_eval_sql_is_template'] = False
            actual = [graph.satisfied(subset) for subset in (set(),{ta},{tb},{ta,tb})]
            ok = (actual == expected and set(graph.inputs) == expected_inputs and details['n_answers'] == 1)
            report['self_tests'].append({'name': name, 'passed': ok, 'truth_table': actual,
                                         'expected': expected, 'n_answers': details['n_answers'],
                                         'backend': details['provsql_export_backend'], 'n_inputs': len(graph.inputs)})
            if not ok:
                raise KCError('Provenance semantic self-test failed: %s; pricing was not started' % name)
        report.pop('self_test_current', None)
        report['status'] = 'SELF_TEST_OK'
        report['live_semantics_verified'] = True
        report['note'] = 'Four live provenance checks passed. This is not an end-to-end c2d/GQR benchmark.'
        return report
    except Exception as exc:
        report['status'] = 'ERROR'
        report['error'] = str(exc)
        if isinstance(exc, LegacyMappingError):
            report['legacy_mapping_check'] = exc.legacy_mapping_check
        if isinstance(exc, LegacyCallbackError):
            report['legacy_callback_check'] = exc.legacy_callback_check
        return report
    finally:
        conn.rollback()
        conn.close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "price", "run", "diagnose"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--scenario", default="scenarios/Deep100")
    parser.add_argument("--size", default="medium")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--provsql-backend", choices=['auto','gate-api','legacy-provenance-evaluate'],
                        default=os.environ.get('PROVSQL_BACKEND','auto'))
    parser.add_argument("--legacy-max-chars", type=int, default=int(os.environ.get('LEGACY_MAX_CHARS','16777216')))
    parser.add_argument("--self-test", action="store_true", help="diagnose: test live input/OR/AND/self-join semantics")
    parser.add_argument("--diagnostic-out", type=Path, help="diagnose: write JSON without passwords")
    parser.add_argument("--queries", default="all")
    parser.add_argument("--query", default="1", help="Single query for the price command")
    parser.add_argument("--sql-file", type=Path)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--c2d", default=os.environ.get("C2D_BIN", "c2d"))
    parser.add_argument("--java", default=os.environ.get("JRE", "java"))
    parser.add_argument("--timeout", type=float, default=100)
    parser.add_argument("--gqr-timeout", type=float, default=300)
    parser.add_argument("--provenance-timeout", type=float, default=300)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-gates", type=int, default=2000000, help="0 disables guard; guard never truncates")
    parser.add_argument("--prices-csv", type=Path)
    parser.add_argument("--unit-price", default="1")
    parser.add_argument("--price-seed", help="Explicit synthetic integer 1..10 price policy")
    parser.add_argument("--reuse-sql", action="store_true")
    parser.add_argument("--regenerate-tgds", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--refresh-mappings", action="store_true")
    parser.add_argument("--rebuild-db", action="store_true")
    parser.add_argument("--confirm-rebuild", default="")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--no-prune", action="store_true")
    parser.add_argument("--check-decomposable", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        os.environ['PROVSQL_BACKEND'] = args.provsql_backend
        os.environ['LEGACY_MAX_CHARS'] = str(args.legacy_max_chars)
        if args.legacy_max_chars < 0:
            raise KCError('--legacy-max-chars must be nonnegative')
        if args.runs < 1 or args.batch_size < 1 or args.max_gates < 0:
            raise KCError("Invalid runs, batch size or gate limit")
        for seconds in (args.timeout, args.gqr_timeout, args.provenance_timeout):
            Deadline(seconds)
        dependencies()
        root, scenario, relative = get_paths(args)
        config = read_pg_config(scenario / "postgres-config.ini")
        print("Database: %s @ %s:%s; schema=%s" %
              (config["dbname"], config["host"], config["port"], args.schema), flush=True)
        if args.command == "diagnose":
            report = diagnose(config, scenario, args.size, args.schema, args.self_test,
                              args.provenance_timeout, args.batch_size)
            if args.diagnostic_out:
                write_json(args.diagnostic_out, report)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 1 if report['status'] == 'ERROR' else 0
        if args.command == "prepare":
            print(json.dumps(prepare(config, scenario, args.size, args.schema, args.refresh_mappings), indent=2))
            return 0
        executable(args.c2d)  # fail before a requested destructive database rebuild
        if args.rebuild_db and args.command != "run":
            raise KCError("Only the run command may rebuild a database")
        setup = {}
        if args.rebuild_db:
            t = time.perf_counter()
            rebuild_database(config, scenario, args.size, args.schema, args.confirm_rebuild)
            setup["data_load_seconds"] = time.perf_counter() - t
        if args.command == "run" and not args.skip_prepare:
            setup.update(prepare(config, scenario, args.size, args.schema, args.refresh_mappings))
        ids = query_ids(scenario, args.queries if args.command == "run" else args.query)
        db_mode = "rebuilt_from_csv" if args.rebuild_db else "reuse_existing_database"
        batch_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        failed = False
        for query in ids:
            query_base = root / "experiments" / "kcprice" / relative / args.size / ("Q" + query)
            batch = query_base / batch_id
            batch.mkdir(parents=True, exist_ok=False)
            rewrite = {}
            try:
                if args.command == "run":
                    sql_file, rewrite = obtain_gqr_sql(root, scenario, relative, query, batch, args)
                else:
                    sql_file = args.sql_file or (root / "experiments" / "outputs" / "gqr" / relative /
                                               "rewritings" / ("Q%s-SQL.txt" % query))
                    if not sql_file.is_file():
                        raise KCError("Raw GQR SQL file not found: %s" % sql_file)
                    copied = batch / "query.sql"
                    copied.write_text(validate_sql(sql_file.read_text(encoding="utf-8")) + ";\n", encoding="utf-8")
                    sql_file = copied
                    rewrite["rewrite_mode"] = "provided_sql"
                write_json(batch / "setup.json", dict(setup, **rewrite))
                for run in range(1, args.runs + 1):
                    out = batch / ("run_%03d" % run)
                    out.mkdir()
                    print("KCPrice Q%s run %d/%d" % (query, run, args.runs), flush=True)
                    metadata = dict(setup, **rewrite, batch_id=batch_id, query="Q" + query, run=run,
                                    scenario=str(relative), data_size=args.size, db_mode=db_mode)
                    result = price_one(config, scenario, sql_file, out, args, metadata)
                    append_summary(query_base / "KCPrice.csv", result)
                    print("status=%s price=%s; %s" % (result["status"], result.get("price"), out / "result.json"), flush=True)
                    if result["status"] != "EXACT":
                        failed = True
                        print(result.get("error", "Failed"), file=sys.stderr)
                        if not args.continue_on_error:
                            return 1
            except (KCError, OSError, ValueError) as exc:
                status = "TIMEOUT" if isinstance(exc, DeadlineExceeded) else "ERROR"
                record = dict(batch_id=batch_id, query="Q" + query, status=status, price=None,
                              error=str(exc), result_directory=str(batch), db_mode=db_mode)
                write_json(batch / "pipeline_error.json", record)
                append_summary(query_base / "KCPrice.csv", record)
                failed = True
                print("ERROR: %s" % exc, file=sys.stderr)
                if not args.continue_on_error:
                    return 1
        return 1 if failed else 0
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
