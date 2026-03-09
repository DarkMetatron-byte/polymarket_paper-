"""Entry point for polymarket-engine.

This is intentionally small and transparent.
It calls the scanner to update the market cache, then runs the paper trader.

Env vars (see config.py for full list):
- PM_DATA_DIR        Base directory for all runtime files
- PM_STATE_PATH      Override paper_state.json location
- PM_DASHBOARD_PATH  Override dashboard.html location
- PM_CACHE_PATH      Override markets_cache.json location

NOTE: The original project lived in /polymarket_paper. See INVENTORY.md and legacy/.
"""

from __future__ import annotations

import logging
import os
import sys

# Ensure app/ is on sys.path so sibling modules are importable regardless of
# the working directory from which this script is launched (e.g. via cron/systemd).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner import main as scanner_main
from trader import main as trader_main

log = logging.getLogger(__name__)


def main() -> None:
    try:
        scanner_main()
    except Exception as exc:
        log.error("Scanner failed (%s) — continuing with cached market data.", exc)
    trader_main()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    main()
