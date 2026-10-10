# Query Pricing Artifact


## 1. Requirements

| Component | Requirement |
| --- | --- |
| Full database experiments | Python 3.12 |
| Python packages | Exact direct versions in `requirements.txt` |
| Query rewriting and data generation | Java 17 or later; `java` on PATH or `JRE` set to its executable |
| Database | PostgreSQL 12 |
| KCPrice | A working c2d executable for the current operating system |
| Offline JSON comparisons | Python and PySAT; c2d when KCPrice is selected; no database or Java required |


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



To run a custom immutable provenance JSON:

```bash
python utilityTools/pricing/pricing_suite.py compare \
  --input utilityTools/pricing/examples/shared_tuple.json \
  --out experiments/custom-comparison \
  --methods KCPrice,SATPrice,SATPrice-NoReduction,Baseline \
  --c2d "$C2D_BIN" --runs 5 --warmups 1 --timeout 3600
```


## 3. Install the benchmark resources

```bash
python scripts/fetch_benchmarks.py
```

This installs 70 explicitly listed files for `Deep100`, `npd`, `Adolena`,
`OWL2Bench`, `StockExchange`, and `Vicodi`: source/target schemas, five queries
per scenario, source/target dependencies, GQR, the long-TGD generator, and the
CSV generator with its configuration.

```bash
# Install only one scenario and the shared runtime files.
python scripts/fetch_benchmarks.py --scenarios Vicodi

# Offline import from an already downloaded copy of the pinned resources.
python scripts/fetch_benchmarks.py --from-directory /path/to/resource-copy
```


## 4. Configure PostgreSQL and ProvSQL




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


## 6. Compare data sizes, methods, and prices

```bash
DATA_SIZES="small medium" \
PRICING_METHODS=KCPrice,SATPrice,SATPrice-NoReduction,Baseline \
PRICE_MODE=all PRICE_SEED=2026 PRICE_TIMEOUT=3600 \
REUSE_SQL=0 WARMUP_RUNS=1 NUM_TESTS=5 CONTINUE_ON_ERROR=1 \
bash scripts/runTrivialMapping_original.sh scenarios/Vicodi "1,2,3,4,5"
```


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
