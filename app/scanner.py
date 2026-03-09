"""Market discovery CLI.

Fetches active markets from Polymarket Gamma API, filters for crypto "Up or Down",
prints a compact pricing view, and writes markets_cache.json.

Usage:
  python3 scanner.py

Env vars:
  PM_DATA_DIR    Base directory for data files (default: directory of this file)
  PM_CACHE_PATH  Override cache path (default: <PM_DATA_DIR>/markets_cache.json)
"""

from __future__ import annotations

import json
import os
import time

from config import CACHE_PATH
from polymarket_client import (
    DEFAULT_BASE_URL,
    GammaClient,
    discover_active_crypto_updown_markets,
    summarize_prices,
)


def main() -> int:
    c = GammaClient()
    markets = discover_active_crypto_updown_markets(c, pages=5, page_size=100)

    os.makedirs(os.path.dirname(os.path.abspath(CACHE_PATH)), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": DEFAULT_BASE_URL,
                "count": len(markets),
                "markets": markets,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    for row in summarize_prices(markets, limit=50):
        print(json.dumps(row, ensure_ascii=False))

    print(f"\nFound {len(markets)} candidate crypto Up/Down markets.")
    print(f"Wrote {CACHE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
