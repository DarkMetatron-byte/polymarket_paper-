#!/bin/bash
# Polymarket Paper Trader — server loop runner
#
# Runs app/main.py every 15 minutes and appends stdout/stderr to a log file.
# For production VPS prefer systemd (scripts/polymarket.service + polymarket.timer).
#
# Usage:
#   PM_DATA_DIR=/var/lib/polymarket bash scripts/run_server.sh
#   nohup bash scripts/run_server.sh &

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="${PM_DATA_DIR:-$ROOT_DIR}"
PYTHON="${ROOT_DIR}/.venv/bin/python"
LOG_FILE="${DATA_DIR}/polymarket.log"
PID_FILE="${DATA_DIR}/polymarket.pid"

mkdir -p "$DATA_DIR"
echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"; echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] polymarket: stopped (pid=$$)" >> "$LOG_FILE"' EXIT

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] polymarket: starting (pid=$$)" | tee -a "$LOG_FILE"

while true; do
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] polymarket: cycle start" >> "$LOG_FILE"
    PM_DATA_DIR="$DATA_DIR" "$PYTHON" "$ROOT_DIR/app/main.py" >> "$LOG_FILE" 2>&1 \
        || echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] polymarket: WARNING — cycle exited non-zero ($?)" >> "$LOG_FILE"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] polymarket: sleeping 900s" >> "$LOG_FILE"
    sleep 900
done
