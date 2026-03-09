"""Entry point for polymarket-engine.

This is intentionally small and transparent.
It calls the scanner to update markets, then runs the paper trader.

Env vars (kept compatible with original code where possible):
- PM_STATE_PATH (default: paper_state.json)
- PM_DASHBOARD_PATH (default: dashboard.html)

NOTE: The original project lived in /polymarket_paper. See INVENTORY.md and legacy/.
"""

from __future__ import annotations

from scanner import main as scanner_main
from trader import main as trader_main


def main() -> None:
    scanner_main()
    trader_main()


if __name__ == "__main__":
    main()
