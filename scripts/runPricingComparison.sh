#!/usr/bin/env bash
# Non-destructive comparison entry. Reuses current DB; provenance exported once.
# Usage: runPricingComparison.sh SCENARIO SIZE QUERIES [extra Python options]
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  cat <<'EOF'
Usage: ./runPricingComparison.sh [scenarios/Deep100] [medium] [1|1,2,3|all] [extra options]
  PRICING_METHODS=SATPrice,SATPrice-NoReduction,Baseline   optional add ,KCPrice
  NUM_TESTS=1 WARMUP_RUNS=0 PRICE_TIMEOUT=100 REUSE_SQL=1 UNIT_PRICE=1
  STATS_REFERENCE=Baseline (all method pairs are still saved)
  PRICE_MODE=all|uniform|random|skewed|heavy-tailed PRICE_SEED=2026 PRICE_DECIMALS=3
  Defaults: all four price modes, one shared provenance export per query
  PRICE_SKEW_PROB=0.9 PRICE_SKEW_CUTOFF=1 PRICE_PARETO_ALPHA=1.5 PRICE_PARETO_MIN=1
  PRICE_CSV=/path/to/prices.csv (exclusive with explicit PRICE_MODE; seed ignored)
  PYTHON=/path/to/conda/env/bin/python PROVSQL_BACKEND=legacy-provenance-evaluate
  MAX_SUPPORTS=200000 MAX_CANDIDATES=5000000 (0 disables each Baseline guard)
  SAT_ENCODING=tseitin (optional preserve-cnf for already positive CNF examples)
  SKIP_PREPARE=1 CONTINUE_ON_ERROR=1
No database rebuild. No-reduction disables paper rules, not the solver's native search.
EOF
  exit 0
fi
if [[ "${REBUILD_DB:-0}" == 1 ]]; then
  echo 'ERROR: comparison runner does not rebuild/drop databases; load data separately.' >&2
  exit 1
fi
BASE_DIR="${1:-scenarios/Deep100}"
DATA_SIZE="${2:-medium}"
QUERIES="${3:-1}"
shift "$(( $# < 3 ? $# : 3 ))"
C2D="${C2D_BIN:-$ROOT_DIR/utilityTools/kcprice/c2d}"
ARGS=(run --root "$ROOT_DIR" --scenario "$BASE_DIR" --size "$DATA_SIZE" --queries "$QUERIES"
      --methods "${PRICING_METHODS:-SATPrice,SATPrice-NoReduction,Baseline}"
      --runs "${NUM_TESTS:-1}" --warmups "${WARMUP_RUNS:-0}" --stats-reference "${STATS_REFERENCE:-Baseline}" --timeout "${PRICE_TIMEOUT:-100}"
      --gqr-timeout "${GQR_TIMEOUT:-300}" --provenance-timeout "${PROVENANCE_TIMEOUT:-300}"
      --c2d "$C2D" --schema "${SOURCE_SCHEMA:-public}" --unit-price "${UNIT_PRICE:-1}"
      --encoding "${SAT_ENCODING:-tseitin}"
      --max-supports "${MAX_SUPPORTS:-200000}" --max-candidates "${MAX_CANDIDATES:-5000000}")
[[ -z "${PRICE_MODE:-}" ]] || ARGS+=(--price-mode "$PRICE_MODE")
[[ -z "${PRICE_CSV:-}" ]] || ARGS+=(--prices-csv "$PRICE_CSV")
[[ -z "${PRICE_SEED:-}" ]] || ARGS+=(--price-seed "$PRICE_SEED")
[[ "${REUSE_SQL:-0}" != 1 ]] || ARGS+=(--reuse-sql)
[[ "${REGENERATE_TGDS:-0}" != 1 ]] || ARGS+=(--regenerate-tgds)
[[ "${SKIP_PREPARE:-0}" != 1 ]] || ARGS+=(--skip-prepare)
[[ "${CONTINUE_ON_ERROR:-0}" != 1 ]] || ARGS+=(--continue-on-error)
exec "${PYTHON:-python}" "$ROOT_DIR/utilityTools/pricing/forbackbench_pricing.py" "${ARGS[@]}" "$@"
