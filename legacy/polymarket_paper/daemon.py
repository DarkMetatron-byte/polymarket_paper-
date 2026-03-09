"""Polymarket paper-trader daemon.

Goals / constraints:
- DO NOT touch OpenClaw container entrypoint or startup logic.
- Run as a separate process (e.g. docker-compose sidecar service, tmux, nohup).
- Periodically:
  1) run market_scan.py
  2) run trader.py in one-shot mode

Notes:
- Uses a lockfile to avoid concurrent daemons.
- Uses a simple activity heuristic: if there are open positions, scan faster.
"""

from __future__ import annotations

import atexit
import datetime as _dt
import json
import os
import subprocess
import time
from pathlib import Path

BASE = Path("/data/.openclaw/workspace/polymarket_paper")
SCAN_INTERVAL = 900  # 15 minutes
FAST_INTERVAL = 120  # 2 minutes when positions are open
LOCKFILE = BASE / "daemon.lock"
CACHE_FILE = BASE / "market_cache.json"  # optional


def log(msg: str) -> None:
    ts = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run(cmd: str) -> None:
    log(f"run: {cmd}")
    # Close stdin so nothing tries to read from it (avoids stray "nohup: ignoring input"-style noise)
    r = subprocess.run(cmd, shell=True, cwd=str(BASE), stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f"command failed rc={r.returncode}: {cmd}")


def scan_markets() -> None:
    run(".venv/bin/python market_scan.py")


def run_trader_once() -> None:
    # trader.py supports --once (optional). If not supported, it should ignore it.
    run(".venv/bin/python trader.py --once")


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(data: dict) -> None:
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_activity() -> bool:
    """Simple heuristic: if the paper trader has open positions, scan more frequently."""

    state = BASE / "paper_state.json"
    if not state.exists():
        return False
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        positions = data.get("positions", {})
        return isinstance(positions, dict) and (len(positions) > 0)
    except Exception:
        return False


def create_lock() -> None:
    BASE.mkdir(parents=True, exist_ok=True)

    if LOCKFILE.exists():
        # Best-effort: if lock is stale and pid is gone, replace it.
        try:
            pid = int(LOCKFILE.read_text().strip() or "0")
        except Exception:
            pid = 0

        if pid > 0 and Path(f"/proc/{pid}").exists():
            raise SystemExit("daemon already running")

        log("stale lockfile found; replacing")

    LOCKFILE.write_text(str(os.getpid()), encoding="utf-8")


def remove_lock() -> None:
    try:
        if LOCKFILE.exists():
            LOCKFILE.unlink()
    except Exception:
        pass


def _preflight() -> None:
    # Basic sanity checks with clear errors (prevents crash-loop confusion)
    if not BASE.exists():
        raise SystemExit(f"BASE not found: {BASE}")

    py = BASE / ".venv/bin/python"
    if not py.exists():
        raise SystemExit(f"python venv not found: {py}")

    for f in ("daemon.py", "trader.py", "market_scan.py"):
        p = BASE / f
        if not p.exists():
            raise SystemExit(f"missing file: {p}")


def main() -> int:
    _preflight()

    create_lock()
    atexit.register(remove_lock)

    log("polymarket daemon started")

    while True:
        start = time.time()

        try:
            scan_markets()
            run_trader_once()
        except Exception as e:
            log(f"ERROR: {e}")

        active = detect_activity()
        target_sleep = FAST_INTERVAL if active else SCAN_INTERVAL

        if active:
            log("active positions detected -> fast scan mode")

        elapsed = time.time() - start
        sleep_time = max(5.0, float(target_sleep) - float(elapsed))
        log(f"sleep {sleep_time:.0f}s")
        time.sleep(sleep_time)


if __name__ == "__main__":
    raise SystemExit(main())
