#!/usr/bin/env bash
# Rebuild runtime statistics from saved results; no solver or DB execution.
# Supply paths relative to your current working directory or absolute paths.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
exec "${PYTHON:-python}" "$ROOT_DIR/utilityTools/pricing/runtime_stats.py" "$@"
