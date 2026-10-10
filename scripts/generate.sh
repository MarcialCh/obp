#!/usr/bin/env bash
# Generate the selected dataset using the pinned upstream generator.
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if (( $# < 2 || $# > 3 )); then
  echo 'Usage: bash scripts/generate.sh scenarios/Deep100 small [oneToOne]' >&2
  exit 2
fi
cd "$ROOT_DIR"
SCENARIO="$1"
SIZE="$2"
MAPPING="${3:-oneToOne}"
case "$SIZE" in small|medium|large|huge) ;; *) echo 'Size must be small, medium, large, or huge.' >&2; exit 2;; esac
[[ "$MAPPING" == oneToOne ]] || { echo 'This artifact supports oneToOne mappings.' >&2; exit 2; }
SCHEMA="$SCENARIO/schema/$MAPPING/s-schema.txt"
JAR='systems/datagenerator/datagenerator-1.01.jar'
[[ -f "$SCHEMA" && -f "$JAR" ]] || { echo 'Fetch benchmark resources first: python scripts/fetch_benchmarks.py' >&2; exit 1; }
DEST="$SCENARIO/data/$MAPPING/$SIZE"
if [[ -d "$DEST" ]] && compgen -G "$DEST/*.csv" >/dev/null && [[ "${REGENERATE_DATA:-0}" != 1 ]]; then
  echo "CSV files already exist in $DEST; set REGENERATE_DATA=1 to regenerate." >&2
  exit 1
fi
mkdir -p "$DEST"
exec "${JRE:-java}" -jar "$JAR" "$SCHEMA" "$DEST" systems/datagenerator/config.ini "$SIZE"
