"""Polymarket paper-trader daemon.

Thin scheduler that calls app/main.py (scanner + trader) in a loop.
Keeps the lock, activity detection, and adaptive interval from the legacy daemon.

Goals / constraints:
- DO NOT touch OpenClaw container entrypoint or startup logic.
- Periodically run app/main.py which handles scanner + trader in one shot.
- When positions are open, scan faster (every 2 min vs 15 min).
- After each cycle, copy runtime files (dashboard, state, cache) to the
  dashboard container's serving directory so they are visible at :8000.

Environment
───────────
The trader container workspace is at BASE (below).  The dashboard container
serves a *different* bind-mount.  To bridge the two we mount that runtime
directory into the trader container and copy files after each cycle.

  docker-compose volume for the trader service:
    /docker/openclaw-rrhx/data/polymarket-engine/runtime:/data/runtime

  RUNTIME_DIR (below) = /data/runtime   (inside the trader container)
"""

from __future__ import annotations

import atexit
import datetime as _dt
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

BASE = Path("/data/.openclaw/workspace/polymarket_paper")
APP_DIR = BASE / "app"
SCAN_INTERVAL = 900    # 15 minutes
FAST_INTERVAL = 120    # 2 minutes when positions are open
LOCKFILE = BASE / "daemon.lock"

# Dashboard webserver container serves from this directory.
# Mount it into the trader container via docker-compose, e.g.:
#   /docker/openclaw-rrhx/data/polymarket-engine/runtime:/data/runtime
RUNTIME_DIR = Path(os.environ.get("PM_RUNTIME_DIR", "/data/runtime"))

# Files to copy to the runtime directory after each cycle.
_PUBLISH_FILES = ("dashboard.html", "paper_state.json", "markets_cache.json")


def log(msg: str) -> None:
    ts = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run(cmd: str, env_extra: dict[str, str] | None = None) -> None:
    log(f"run: {cmd}")
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        cmd, shell=True, cwd=str(BASE), stdin=subprocess.DEVNULL, env=env,
    )
    if r.returncode != 0:
        log(f"ERROR: command failed rc={r.returncode}: {cmd}")


def run_cycle() -> None:
    """Run one scanner+trader cycle via app/main.py."""
    run(
        ".venv/bin/python app/main.py",
        env_extra={"PM_DATA_DIR": str(BASE)},
    )


def publish_to_runtime() -> None:
    """Copy dashboard / state / cache to the runtime dir served by :8000."""
    if not RUNTIME_DIR.exists():
        log(f"RUNTIME_DIR not found ({RUNTIME_DIR}) — skipping publish")
        return
    copied = []
    for name in _PUBLISH_FILES:
        src = BASE / name
        if src.exists():
            try:
                shutil.copy2(str(src), str(RUNTIME_DIR / name))
                copied.append(name)
            except Exception as exc:
                log(f"WARNING: copy {name} -> {RUNTIME_DIR}: {exc}")
    if copied:
        log(f"published to runtime: {', '.join(copied)}")


def detect_activity() -> bool:
    """If the paper trader has open positions, scan more frequently."""
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
    if not BASE.exists():
        raise SystemExit(f"BASE not found: {BASE}")

    py = BASE / ".venv/bin/python"
    if not py.exists():
        raise SystemExit(f"python venv not found: {py}")

    main_py = APP_DIR / "main.py"
    if not main_py.exists():
        raise SystemExit(f"missing entry point: {main_py}")

    trader_py = APP_DIR / "trader.py"
    if not trader_py.exists():
        raise SystemExit(f"missing trader: {trader_py}")

    if not RUNTIME_DIR.exists():
        log(f"WARNING: RUNTIME_DIR ({RUNTIME_DIR}) does not exist yet. "
            "Dashboard files will not be published until the mount is added.")


def main() -> int:
    _preflight()

    create_lock()
    atexit.register(remove_lock)

    log(f"polymarket daemon started  BASE={BASE}  RUNTIME={RUNTIME_DIR}")

    while True:
        start = time.time()

        try:
            run_cycle()
        except Exception as e:
            log(f"ERROR: {e}")

        # Copy dashboard + state files to the webserver's serving directory.
        try:
            publish_to_runtime()
        except Exception as e:
            log(f"ERROR publishing: {e}")

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
