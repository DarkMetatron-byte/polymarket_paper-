"""Binance Spot API client — market data only (no auth required).

Mirrors the GammaClient pattern from polymarket_client.py:
same @dataclass layout, private _get(), custom exception class.
No external deps — urllib, json, dataclasses only.

Docs: https://developers.binance.com/docs/binance-spot-api-docs
Base URL: https://api.binance.com
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


BINANCE_BASE_URL = "https://api.binance.com"

# Asset key → Binance ticker (only what Polymarket tracks).
TRACKED_TICKERS: Dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}


class BinanceAPIError(RuntimeError):
    pass


@dataclass
class BinanceClient:
    base_url:   str = BINANCE_BASE_URL
    timeout_s:  int = 10
    user_agent: str = "polymarket-paper/0.1"

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: str(v) for k, v in params.items()}
            )

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
            raise BinanceAPIError(f"GET {url} failed: {e}") from e

    def get_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Fetch latest spot prices for multiple symbols in one request.

        Uses GET /api/v3/ticker/price?symbols=["BTCUSDT","ETHUSDT","SOLUSDT"]
        Returns dict mapping ticker symbol → float price.

        API weight: 4 (multi-symbol form).
        """
        # Binance expects the `symbols` query parameter as a JSON array string.
        # Important: no spaces inside the JSON, otherwise some servers reject it.
        # Example: symbols=["BTCUSDT","ETHUSDT","SOLUSDT"]
        params = {"symbols": json.dumps(symbols, separators=(",", ":"))}
        data = self._get("/api/v3/ticker/price", params=params)

        if not isinstance(data, list):
            raise BinanceAPIError(
                f"Unexpected /api/v3/ticker/price response shape: {type(data)}"
            )

        out: Dict[str, float] = {}
        for item in data:
            if isinstance(item, dict) and "symbol" in item and "price" in item:
                try:
                    out[str(item["symbol"])] = float(item["price"])
                except (ValueError, TypeError):
                    pass
        return out

    def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 8,
    ) -> List[List]:
        """Fetch candlestick / kline data for a symbol.

        Each kline is a list:
          [openTime, open, high, low, close, volume, closeTime, ...]

        API weight: 2.
        """
        params: Dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        data = self._get("/api/v3/klines", params=params)

        if not isinstance(data, list):
            raise BinanceAPIError(
                f"Unexpected /api/v3/klines response shape: {type(data)}"
            )

        return data
