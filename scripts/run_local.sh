#!/bin/bash
# Polymarket Paper Trader — local dev one-shot runner.
# Runs a single cycle of scanner + trader and exits.
# No supervisor or systemd required.
#
# Usage:
#   bash scripts/run_local.sh
#   PM_DATA_DIR=/tmp/pm-dev bash scripts/run_local.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -x "${ROOT_DIR}/.venv/bin/python" ]; then
    PYTHON="${ROOT_DIR}/.venv/bin/python"
else
    PYTHON="python3"
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] polymarket: one-shot run (local dev)"
"$PYTHON" "$ROOT_DIR/app/main.py"
