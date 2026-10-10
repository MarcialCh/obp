# Query Pricing Artifact

Anonymous research artifact for comparing **KCPrice**, **SATPrice**,
**SATPrice-NoReduction**, and **Baseline** on one shared query-answer
provenance snapshot. This is an integrated source release: it includes the
algorithms, database adapter, dataset loader, experiment runners, reporting
code, examples, and regression tests. All documentation and source comments
are in English.

External dependencies are installed separately: Python packages, c2d,
PostgreSQL with ProvSQL, and the pinned ForBackBench resources. The included
setup tools download or import those resources. No previous patch archive is
required. See [third-party dependencies](THIRD_PARTY.md) for sources and terms.

## 1. Requirements

| Component | Requirement |
| --- | --- |
| Full database experiments | Linux, Bash, Python 3.10 or later; Python 3.11 is the validation target |
| Python packages | Exact direct versions in `requirements.txt` |
| Query rewriting and data generation | Java 17 or later; `java` on PATH or `JRE` set to its executable |
| Database | A dedicated PostgreSQL instance with a compatible ProvSQL extension; PostgreSQL 12 was used in the original deployment |
| KCPrice | A working c2d executable for the current operating system |
| Offline JSON comparisons | Python and PySAT; c2d when KCPrice is selected; no database or Java required |

Windows supports the offline Python comparisons. The database adapter uses
POSIX file locks and the full experiment pipeline requires Linux. Native
c2d support depends on the supplied executable and operating system; use
Linux for database benchmarks and read the validation notes below.

## 2. Quick start: offline algorithms

From the extracted repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# Runs three methods immediately, using a bundled JSON example.
python scripts/run_demo.py --methods SATPrice,SATPrice-NoReduction,Baseline
```

For all four methods, obtain c2d from its
[official download page](http://reasoning.cs.ucla.edu/c2d/download.php), retain
its original license, and install the executable locally:

```bash
python scripts/install_c2d.py /path/to/downloaded/c2d
export C2D_BIN="$PWD/utilityTools/kcprice/c2d"
python scripts/run_demo.py --runs 2 --warmups 1
```

On Windows, install the Windows executable with the same Python tool, set
`C2D_BIN` to its absolute path, and run the Python entry points directly.
`dependencies/c2d.reference.json` records the reference executable hashes.
The reference Linux binary is 32-bit x86 and needs the corresponding runtime
loader on the host; alternatively use an appropriate official build.

The example has answers `(x OR a)` and `(x OR b)`, with prices `x=3`, `a=2`,
and `b=2`. **Every method must return the joint price 3**, buying `x` once.
`consistency.json` should report `AGREE`. Each run uses a fresh worker process.
Results are written under `experiments/demo/<batch>/`.

To run a custom immutable provenance JSON:

```bash
python utilityTools/pricing/pricing_suite.py compare \
  --input utilityTools/pricing/examples/shared_tuple.json \
  --out experiments/custom-comparison \
  --methods KCPrice,SATPrice,SATPrice-NoReduction,Baseline \
  --c2d "$C2D_BIN" --runs 5 --warmups 1 --timeout 3600
```

Choose a new output directory for each comparison. The offline JSON interface
uses the prices already stored in that JSON; automatic price-mode iteration
belongs to the database experiment runner.

## 3. Install the benchmark resources

```bash
python scripts/fetch_benchmarks.py
```

This installs 70 explicitly listed files for `Deep100`, `npd`, `Adolena`,
`OWL2Bench`, `StockExchange`, and `Vicodi`: source/target schemas, five queries
per scenario, source/target dependencies, GQR, the long-TGD generator, and the
CSV generator with its configuration. The manifest
`dependencies/forbackbench.lock.json` pins a Git commit, file sizes, Git blob
hashes, and SHA256 digests. Each file is checked before installation. A failed
download can be resumed by running the same command again.

```bash
# Install only one scenario and the shared runtime files.
python scripts/fetch_benchmarks.py --scenarios Vicodi

# Offline import from an already downloaded copy of the pinned resources.
python scripts/fetch_benchmarks.py --from-directory /path/to/resource-copy
```

Existing files with different contents are rejected. Use `--replace` only
when intentionally restoring the pinned resources. This does not download
database credentials, cached rewritten SQL, precomputed long TGDs, or
generated CSVs.

## 4. Configure PostgreSQL and ProvSQL

Use a **dedicated experimental database**. The full-size runner drops and
recreates the database named by `PGDATABASE` for every selected data size.
It checks for other connections and refuses to forcibly terminate them.
Setup failures stop the batch even with `CONTINUE_ON_ERROR=1`.

Install ProvSQL for the PostgreSQL server version that will run the experiment.
[The pinned legacy build instructions](docs/PROVSQL_SETUP.md) are included.
If a compatible server is already configured, retain it and use the live
diagnostic command below after loading a dataset.

Create password-free connection files:

```bash
python scripts/configure_database.py --host localhost --port 5433 --user postgres
```

This creates `scenarios/<scenario>/postgres-config.ini`, with database names
such as `pricing_vicodi`. Existing connection files are retained. Edit the
file to match the dedicated server; this assignment-style format is not an
INI section format:

```ini
PGHOST=localhost
PGPORT=5433
PGUSER=postgres
PGDATABASE=pricing_vicodi
```

Use libpq's `.pgpass` or the `PGPASSWORD` environment variable for local
authentication. Connection files and credentials are excluded by `.gitignore`.
The database role must be able to recreate its experimental database, import
tables, and install the server's ProvSQL extension.

## 5. Generate different dataset sizes

```bash
bash scripts/generate.sh scenarios/Vicodi small oneToOne
bash scripts/generate.sh scenarios/Vicodi medium oneToOne
```

The pinned generator configuration uses these requested row counts:

| Size | Unary source relation | Binary source relation |
| --- | ---: | ---: |
| `small` | 500 | 1,000 |
| `medium` | 5,000 | 10,000 |
| `large` | 50,000 | 100,000 |
| `huge` | 500,000 | 1,000,000 |

The fourth size is named `huge`, not `very large`. For relations with other
arities, consult the upstream generator and inspect the load manifest; the
table above describes unary/binary settings. Actual total rows depend on the
scenario. The generator configuration lives in
`systems/datagenerator/config.ini` after resource installation.

Generated CSVs are retained for reuse. Existing CSVs require an explicit
`REGENERATE_DATA=1` to regenerate. `PRICE_SEED` controls tuple prices only;
it does not set the upstream data generator's random seed. Preserve the CSVs
and `load_manifest.json` to reproduce the same data and its fingerprint.

## 6. Compare data sizes, methods, and prices

```bash
DATA_SIZES="small medium" \
PRICING_METHODS=KCPrice,SATPrice,SATPrice-NoReduction,Baseline \
PRICE_MODE=all PRICE_SEED=2026 PRICE_TIMEOUT=3600 \
REUSE_SQL=0 WARMUP_RUNS=1 NUM_TESTS=5 CONTINUE_ON_ERROR=1 \
bash scripts/runTrivialMapping_original.sh scenarios/Vicodi "1,2,3,4,5"
```

The sequence for each size is:

1. Validate the selected CSV directories and price configuration.
2. Acquire the experiment lock; rebuild and load that size's database.
3. Record row counts and a dataset fingerprint; prepare source provenance.
4. For each query, rewrite once, retrieve the full answer set once, and export
   its complete supported provenance DAG once.
5. Apply each selected price policy to the same immutable DAG and run all
   selected methods, warmups, and measured repetitions.
6. Save per-mode comparisons and the shared batch report.

The full-size entry defaults to `small medium`, all four algorithms, all four
price modes, seed `2026`, one warmup, five measured runs, and `REUSE_SQL=0`.
The separate `runPricingComparison.sh` entry retains its lighter defaults of
three non-KC methods, one measured run, and no warmup; set the environment
variables explicitly for comparable experiments.

For synthetic NPD data, repeated business keys and generated text can conflict
with the original schema. Preserve every generated tuple with:

```bash
DATA_SIZES="small medium" SOURCE_KEY_MODE=surrogate SOURCE_TEXT_MODE=unbounded \
PRICING_METHODS=KCPrice,SATPrice,SATPrice-NoReduction,Baseline \
PRICE_TIMEOUT=3600 REUSE_SQL=0 WARMUP_RUNS=1 NUM_TESTS=5 CONTINUE_ON_ERROR=1 \
bash scripts/runTrivialMapping_original.sh scenarios/npd "1,2,3,4,5"
```

`surrogate` replaces source primary keys with `__fact_id`; it does not discard
duplicate business-key rows. `unbounded` widens built-in bounded character
columns to `text`. Both choices are recorded in the load audit. Their defaults
are `strict`, which preserves the original schema's constraints.

For an already loaded, matching dataset:

```bash
PRICING_METHODS=KCPrice,SATPrice,SATPrice-NoReduction,Baseline \
PRICE_TIMEOUT=3600 REUSE_SQL=0 SKIP_PREPARE=0 \
WARMUP_RUNS=1 NUM_TESTS=5 CONTINUE_ON_ERROR=1 \
bash scripts/runPricingComparison.sh scenarios/Vicodi medium "1,2,3,4,5"

bash scripts/checkProvSQL_KCPrice.sh scenarios/Vicodi medium
```

This entry reuses the database and does not reload a different size merely
because its size argument changes. Use the full-size entry for size comparisons.
`SKIP_PREPARE=1` is appropriate only after the matching source tables and
provenance mappings have been prepared.

## 7. Price policies and limits

| Setting | Default / meaning |
| --- | --- |
| `PRICE_MODE` | `all`: `uniform`, `random`, `skewed`, `heavy-tailed`, in that order |
| `PRICE_SEED` | `2026` |
| `uniform` | Exactly 1 for every tuple |
| `random` | Uniform decimal-grid samples strictly inside `(0,10)` |
| `skewed` | Probability 0.9 in `(0,1)`, otherwise in `[1,10)` |
| `heavy-tailed` | Pareto Type I, minimum 1 and shape 1.5; values above 10 are allowed |
| `PRICE_DECIMALS` | 3; supported range 1 to 6 |
| `PRICE_SKEW_PROB`, `PRICE_SKEW_CUTOFF` | 0.9 and 1 |
| `PRICE_PARETO_ALPHA`, `PRICE_PARETO_MIN` | 1.5 and 1 |
| `PRICE_TIMEOUT` | 100 seconds per complete isolated pricing worker; set 3600 for one hour |
| `GQR_TIMEOUT` | 300 seconds per external rewriting/generation process |
| `PROVENANCE_TIMEOUT` | 300 seconds for answer retrieval plus provenance export; tuple resolution has its own budget of this length |
| `MAX_SUPPORTS`, `MAX_CANDIDATES` | Baseline guards: 200,000 and 5,000,000; 0 disables a guard |
| `LEGACY_MAX_CHARS` | 16,777,216 for legacy formula text; 0 disables the guard |

Random prices are derived from the seed, schema-qualified relation, and tuple
identifier. Query number, run order, and fresh provenance UUIDs do not change
the price of that same tuple identity. Exact decimals are scaled to integer
costs for optimization. Heavy-tailed samples are rounded half-up; overflow is
an error, not a silent cap.

Select one mode with `PRICE_MODE=random`, for example. An explicit `PRICE_CSV`
is a single alternative policy and cannot be combined with an explicit
`PRICE_MODE`. Its columns are `relation,tuple_id,price`; relation and tuple ID
must match the exported catalog. Uniform prices ignore the seed.

There is no single timeout covering the entire batch. Warmups, repeats,
queries, modes, rewriting, and export each add time. Guards and timeouts
produce failure statuses and a missing price; no partial optimum is reported.

## 8. Output layout and timing definitions

Paths are relative to the repository root:

```text
experiments/pricing/scenarios/<scenario>/<size>/
  loads/<load-batch>/load_manifest.json
  Q1/<batch>/
    export/                         # shared graph, catalog, query metadata
    price_modes/<mode>/comparison/  # isolated method runs and mode report
    price_modes/<mode>/export/      # repriced exports for non-uniform modes
  runtime/<batch>/
    runtime_summary.csv
    runtime_report.txt
    runtime_shared.csv
    runtime_statistics.json
```

For a single explicit price mode, the query-level comparison uses
`Q1/<batch>/comparison/`. The combined report remains under
`runtime/<batch>/`; it includes all queries and price modes in that invocation.

`worker_wall_seconds` is measured by the parent around the entire worker,
including startup, input, algorithm work, verification, and output.
For successful runs, functional stages partition that exact interval:

```text
KCPrice total = c2d_stage_seconds + price_stage_seconds
SATPrice total = reduction_stage_seconds + solver_stage_seconds
```

These are **functional stages**: the c2d stage includes preparation and
compilation-side input work, and the price stage includes NNF evaluation and
result handling. The reduction stage includes input/encoding/preprocessing;
the solver stage includes solver execution and final verification/output.
They are broader than the internal timers `compile_seconds`,
`reduction_seconds`, or `solver_seconds`. Missing stage boundaries are left
blank; times are not fabricated to make failed runs add up.

Shared preprocessing is recorded once per query/batch across all methods,
price modes, and repetitions, including in `runtime_report.txt`:

- `shared_query_rewrite_seconds`: GQR rewriting; reused/provided SQL records 0.
- `shared_query_execution_seconds`: execution and exhaustion of the answer
  cursor, including provenance work the extension performs during the query.
- `shared_provenance_build_seconds`: construction/export of the pricing DAG
  after the answer cursor completes.

These boundaries do not claim to separate the database engine's internal
query work from inline provenance instrumentation. Shared costs are outside
each pricing worker's time. Warmups and failed runs are excluded from timing
means; failures remain counted. Speedups use matched successful runs and
matching exact prices.

Regenerate reports without rerunning algorithms:

```bash
bash scripts/summarizePricingTimes.sh \
  --root experiments/pricing/scenarios/Vicodi/small \
  --out experiments/reports/vicodi-small
```

## 9. Algorithms and supported inputs

The objective is the minimum cost of a set of source tuples that preserves
**all** exported answer roots. A source tuple used by several answers is paid
for once. Supported circuits are positive, monotone provenance DAGs; unsupported
or nonmonotone operators are rejected.

| Method | Implementation |
| --- | --- |
| KCPrice | Boolean encoding, c2d compilation to d-DNNF, then minimum-cost evaluation and support reconstruction |
| SATPrice | Encoding, application-level reduction rules, then PySAT RC2 with result reconstruction |
| SATPrice-NoReduction | Unreduced weighted partial MaxSAT solved by the implemented WPM3 algorithm over a SAT oracle |
| Baseline | Enumerate inclusion-minimal global why-provenance supports using antichain union/product; scan their exact purchase costs |

SATPrice-NoReduction uses WPM3 rather than RC2, so its comparison with SATPrice
changes both preprocessing and optimizer implementation. It should not be
interpreted as a controlled measurement of reduction alone. The WPM3
implementation uses totalizers and does not reproduce the reference solver's
specialized sorting-network optimizations. Baseline is a reimplementation
of the stated enumeration strategy, with no cost-based pruning or SAT solver.

The package retains the fixes for source-table case/quoting, legacy ProvSQL
regclass handling, source keys/text, auxiliary predicate arities, hyphenated
predicate names, source SQL column binding, and size-specific database loads.
Invalid source arities remain errors; changing a schema's arity silently would
change the experiment. Regenerate stale long TGDs with `REGENERATE_TGDS=1`.
