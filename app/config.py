"""Configuration placeholders.

We keep this file so Codex/local dev has a single place to add config later.
Current runtime config is mostly environment variables inside trader.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    state_path: str = "paper_state.json"
    dashboard_path: str = "dashboard.html"
