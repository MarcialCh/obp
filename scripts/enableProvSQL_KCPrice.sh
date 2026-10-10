#!/usr/bin/env bash
# Prepare provenance on already loaded source tables in the configured database.
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${PYTHON:-python}" "$ROOT_DIR/utilityTools/kcprice/forbackbench.py" prepare \
  --root "$ROOT_DIR" --scenario "${1:-scenarios/Deep100}" --size "${2:-small}" \
  --schema "${SOURCE_SCHEMA:-public}"
