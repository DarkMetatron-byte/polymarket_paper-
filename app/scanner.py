"""Market discovery CLI.

Fetches active markets from Polymarket Gamma API, filters for crypto "Up or Down",
prints a compact pricing view, and writes markets_cache.json.

Usage:
  python3 discover_markets.py
"""

from polymarket_client import (
    DEFAULT_BASE_URL,
    GammaClient,
    discover_active_crypto_updown_markets,
    summarize_prices,
)

import json
import time


def main() -> int:
    c = GammaClient()
    markets = discover_active_crypto_updown_markets(c, pages=5, page_size=100)

    with open("markets_cache.json", "w", encoding="utf-8") as f:
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
    print("Wrote markets_cache.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
