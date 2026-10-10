#!/usr/bin/env bash
# Probe the installed extension and verify live provenance semantics.
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${PYTHON:-python}" "$ROOT_DIR/utilityTools/kcprice/forbackbench.py" diagnose \
  --root "$ROOT_DIR" --scenario "${1:-scenarios/Deep100}" --size "${2:-small}" \
  --schema "${SOURCE_SCHEMA:-public}" --self-test
