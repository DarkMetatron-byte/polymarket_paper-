"""Minimal Polymarket Gamma API client + market discovery.

No external deps (uses urllib).

Docs note: Gamma is read-only market metadata.
Base URL: https://gamma-api.polymarket.com
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"


class GammaAPIError(RuntimeError):
    pass


@dataclass
class GammaClient:
    base_url: str = DEFAULT_BASE_URL
    timeout_s: int = 20
    user_agent: str = "polymarket-paper/0.1"

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = params or {}
        url = self.base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as e:
            raise GammaAPIError(f"GET {url} failed: {e}") from e

    def get_markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        archived: bool = False,
        order: str = "volume",
        ascending: bool = False,
        **extra_params: Any,
    ) -> List[Dict[str, Any]]:
        """Fetch markets via /markets.

        Gamma supports pagination via limit/offset.

        Many fields are returned; we keep it generic here.
        """
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "archived": str(archived).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        params.update(extra_params)

        data = self._get("/markets", params=params)
        # Gamma usually returns a list. If it ever returns an object, normalize.
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
            return data["markets"]
        raise GammaAPIError(f"Unexpected /markets response shape: {type(data)}")


def is_up_or_down_crypto_market(m: Dict[str, Any]) -> bool:
    """Heuristic filter for crypto 'Up or Down' markets.

    Polymarket naming varies; we use best-effort matching on question/title.
    """
    text = " ".join(
        str(m.get(k, ""))
        for k in (
            "question",
            "title",
            "description",
            "slug",
        )
    ).lower()

    # core pattern: "up"/"down" style markets, often phrased like
    # "Will Bitcoin be up or down ..." or "Bitcoin: Up or Down ..."
    if "up or down" not in text and ("will" not in text or "up" not in text or "down" not in text):
        return False

    # crypto tickers/names of interest
    crypto_hits = any(x in text for x in ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "\nsol\n"))
    if not crypto_hits:
        return False

    # keep active markets only if field exists
    if "active" in m and not m.get("active"):
        return False

    return True


def discover_active_crypto_updown_markets(
    client: GammaClient,
    *,
    pages: int = 5,
    page_size: int = 100,
    sleep_s: float = 0.2,
) -> List[Dict[str, Any]]:
    """Pull several pages of active markets and filter down."""
    out: List[Dict[str, Any]] = []
    for i in range(pages):
        markets = client.get_markets(limit=page_size, offset=i * page_size, active=True, closed=False)
        for m in markets:
            if is_up_or_down_crypto_market(m):
                out.append(m)
        time.sleep(sleep_s)
    return out


def get_yes_midprice_for_outcome(m: Dict[str, Any], outcome_name: str) -> Optional[float]:
    """Best-effort 'realistic' price:

    - Prefer midprice from orderbook top-of-book if present (bestBid/bestAsk).
    - Fallback to outcomePrices (mark price / implied probability).

    Note: bestBid/bestAsk are for the primary outcome (YES) on binary markets.
    For 'Up or Down' style markets, YES ≈ 'Up' and NO ≈ 'Down'.
    """

    # 1) Orderbook top-of-book mid (more realistic)
    bb = m.get("bestBid")
    ba = m.get("bestAsk")
    try:
        if bb is not None and ba is not None:
            bb_f = float(bb)
            ba_f = float(ba)
            if 0 < bb_f <= 1 and 0 < ba_f <= 1 and bb_f <= ba_f:
                return (bb_f + ba_f) / 2
    except Exception:
        pass

    # 2) Fallback: outcomePrices
    outcomes = m.get("outcomes")
    prices = m.get("outcomePrices")
    try:
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        if isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices):
            if outcome_name in outcomes:
                i = outcomes.index(outcome_name)
                return float(prices[i])
    except Exception:
        return None

    return None


def _parse_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def get_spread(m: Dict[str, Any]) -> Optional[float]:
    """Top-of-book spread for the primary YES outcome, if available."""
    bb = _parse_float(m.get("bestBid"))
    ba = _parse_float(m.get("bestAsk"))
    if bb is None or ba is None:
        return None
    if bb < 0 or ba < 0 or bb > 1 or ba > 1 or bb > ba:
        return None
    return ba - bb


def summarize_prices(markets: List[Dict[str, Any]], *, limit: int = 25) -> List[Dict[str, Any]]:
    """Compact view focusing on pricing fields we care about."""
    out: List[Dict[str, Any]] = []
    for m in markets[:limit]:
        up_price = get_yes_midprice_for_outcome(m, "Up")
        out.append(
            {
                "id": m.get("id"),
                "slug": m.get("slug"),
                "question": m.get("question") or m.get("title"),
                "end": m.get("endDate") or m.get("end_date"),
                "mid_yes_up": up_price,
                "bestBid": m.get("bestBid"),
                "bestAsk": m.get("bestAsk"),
                "spread": get_spread(m),
                "outcomePrices": m.get("outcomePrices"),
            }
        )
    return out


if __name__ == "__main__":
    c = GammaClient()
    markets = discover_active_crypto_updown_markets(c, pages=5, page_size=100)

    # Save cache for the 15-min runner
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

    # Print a compact view for inspection (pricing-focused)
    for row in summarize_prices(markets, limit=25):
        print(json.dumps(row, ensure_ascii=False))

    print(f"\nFound {len(markets)} candidate crypto Up/Down markets.")
    print("Wrote markets_cache.json")
