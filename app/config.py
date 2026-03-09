"""Configuration for polymarket-paper.

All runtime config is controlled by environment variables.
This module resolves and exports the path constants used by trader.py,
scanner.py, and main.py — acting as the single source of truth for paths.

Environment variables
─────────────────────
PM_DATA_DIR
    Base directory for all runtime data files (state, cache, dashboard, logs).
    Default: directory of this file (app/).
    Recommended on a VPS: /var/lib/polymarket

PM_STATE_PATH
    Override the paper trading state JSON file path.
    Default: <PM_DATA_DIR>/paper_state.json

PM_DASHBOARD_PATH
    Override the generated HTML dashboard path.
    Default: <PM_DATA_DIR>/dashboard.html

PM_CACHE_PATH
    Override the market discovery cache JSON file path.
    Default: <PM_DATA_DIR>/markets_cache.json
"""

from __future__ import annotations

import os

# Directory of this file (app/).
APP_DIR: str = os.path.dirname(os.path.abspath(__file__))

# Base directory for all runtime data files.
DATA_DIR: str = os.environ.get("PM_DATA_DIR", APP_DIR)

# Resolved file paths (each can be individually overridden via env var).
STATE_PATH: str = os.environ.get("PM_STATE_PATH", os.path.join(DATA_DIR, "paper_state.json"))
DASHBOARD_PATH: str = os.environ.get("PM_DASHBOARD_PATH", os.path.join(DATA_DIR, "dashboard.html"))
CACHE_PATH: str = os.environ.get("PM_CACHE_PATH", os.path.join(DATA_DIR, "markets_cache.json"))
