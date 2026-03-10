#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

INTERVAL_SECONDS="${INTERVAL_SECONDS:-900}"
IMMEDIATE_FIRST_RUN="${IMMEDIATE_FIRST_RUN:-0}"

cd "${REPO_ROOT}"

echo "[openclaw] aligned service with notify, interval=${INTERVAL_SECONDS}s, python=${PYTHON_BIN}"

send_status() {
  "${PYTHON_BIN}" scripts/telegram_message.py || true
}

send_alert() {
  local msg="$1"
  "${PYTHON_BIN}" scripts/telegram_message.py --text "$msg" || true
}

run_cycle() {
  local now_utc rc
  now_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "[openclaw] cycle start ${now_utc}"

  set +e
  "${PYTHON_BIN}" app/main.py
  rc=$?
  set -e

  if [[ ${rc} -ne 0 ]]; then
    send_alert "Polymarket ALERT\nCycle failed at ${now_utc} UTC\nexit_code=${rc}\nCheck VPS logs." 
    echo "[openclaw] cycle failed rc=${rc}"
    return ${rc}
  fi

  send_status
  echo "[openclaw] cycle success ${now_utc}"
  return 0
}

sleep_until_next_slot() {
  local now next sleep_for
  now="$(date +%s)"
  next=$(( ((now / INTERVAL_SECONDS) + 1) * INTERVAL_SECONDS ))
  sleep_for=$(( next - now ))
  echo "[openclaw] sleeping ${sleep_for}s until $(date -d "@${next}" +"%Y-%m-%d %H:%M:%S %Z")"
  sleep "${sleep_for}"
}

if [[ "${IMMEDIATE_FIRST_RUN}" == "1" ]]; then
  run_cycle || true
fi

while true; do
  sleep_until_next_slot
  run_cycle || true
done
