"""Binance external signal engine.

Fetches real-time BTC/ETH/SOL spot prices + klines from Binance once per cycle
and blends them with the internal p_hat estimate.

For "Up or Down" crypto markets the Binance spot momentum is the *actual*
underlying variable — making it a genuine external signal, unlike the internal
model which only smooths Polymarket's own price history.

Signal formula:
    z         = recent_return / max(price_volatility, 1e-8)
    p_binance = 0.5 + 0.5 * tanh(z * 0.5)    (bounded [0,1])

Blend:
    p_hat_final = blend_weight * p_binance + (1 - blend_weight) * p_internal

Fallback: if Binance is unavailable or asset is not detected from the market
text, p_internal is returned unchanged (graceful degradation).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from binance_client import BinanceClient, TRACKED_TICKERS


# ── Config ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignalConfig:
    """Tunable parameters for the Binance signal layer."""

    kline_interval:        str   = "15m"   # kline timeframe
    kline_count:           int   = 8       # 8 × 15 min = 2 hours of history
    binance_blend_weight:  float = 0.30    # 30 % Binance / 70 % internal
    signal_min:            float = 0.10   # output clamped to [signal_min, signal_max]
    signal_max:            float = 0.90


# ── Keyword map for market → asset detection ───────────────────────────────────

_ASSET_KEYWORDS: Dict[str, List[str]] = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
}


# ── AssetSnapshot ───────────────────────────────────────────────────────────────

@dataclass
class AssetSnapshot:
    """Price + kline snapshot for one tracked asset (BTC, ETH, or SOL)."""

    ticker:        str        # e.g. "BTCUSDT"
    current_price: float      # latest spot price in USDT
    klines:        List[List] # raw kline arrays from Binance API
    fetched_at:    float      # Unix epoch when fetched

    @property
    def recent_return(self) -> Optional[float]:
        """2-hour return: (current_price - open_of_oldest_kline) / open_of_oldest_kline.

        Returns None when klines are empty or the open price is unusable.
        """
        if not self.klines:
            return None
        try:
            oldest_open = float(self.klines[0][1])   # index 1 = open price
            if oldest_open <= 0:
                return None
            return (self.current_price - oldest_open) / oldest_open
        except (IndexError, TypeError, ValueError):
            return None

    @property
    def price_volatility(self) -> Optional[float]:
        """Std-dev of close-to-close returns across the kline window.

        Returns None if fewer than 2 klines are available.
        """
        if len(self.klines) < 2:
            return None
        try:
            closes = [float(k[4]) for k in self.klines]   # index 4 = close price
            returns = [
                (closes[i] - closes[i - 1]) / closes[i - 1]
                for i in range(1, len(closes))
                if closes[i - 1] > 0
            ]
            if not returns:
                return None
            mean = sum(returns) / len(returns)
            variance = sum((r - mean) ** 2 for r in returns) / len(returns)
            return math.sqrt(variance)
        except (IndexError, TypeError, ValueError):
            return None


# ── BinanceSnapshot ─────────────────────────────────────────────────────────────

@dataclass
class BinanceSnapshot:
    """Container for all per-asset snapshots fetched in a single cycle."""

    assets:     Dict[str, AssetSnapshot]  # asset key → snapshot
    errors:     Dict[str, str]            # asset key → error message (for logging)
    fetched_at: float                     # Unix epoch of the fetch

    @classmethod
    def fetch(cls, client: BinanceClient, cfg: SignalConfig) -> "BinanceSnapshot":
        """Fetch spot prices + klines for all TRACKED_TICKERS.

        Batch-fetches all spot prices in one API call, then fetches klines per
        asset individually. Per-asset errors are caught and stored in .errors
        so a single bad asset does not block the others.
        """
        fetched_at = time.time()
        assets: Dict[str, AssetSnapshot] = {}
        errors: Dict[str, str] = {}

        # One batch call for all spot prices (API weight: 4)
        tickers = list(TRACKED_TICKERS.values())
        try:
            prices = client.get_prices(tickers)
        except Exception as exc:
            for asset in TRACKED_TICKERS:
                errors[asset] = f"price fetch failed: {exc}"
            return cls(assets=assets, errors=errors, fetched_at=fetched_at)

        # Per-asset kline fetch (API weight: 2 each)
        for asset, ticker in TRACKED_TICKERS.items():
            try:
                current_price = prices.get(ticker)
                if current_price is None:
                    errors[asset] = f"price missing for {ticker}"
                    continue
                klines = client.get_klines(
                    ticker,
                    interval=cfg.kline_interval,
                    limit=cfg.kline_count,
                )
                assets[asset] = AssetSnapshot(
                    ticker=ticker,
                    current_price=float(current_price),
                    klines=klines,
                    fetched_at=fetched_at,
                )
            except Exception as exc:
                errors[asset] = str(exc)

        return cls(assets=assets, errors=errors, fetched_at=fetched_at)

    def get_asset_snapshot(self, market: Dict[str, Any]) -> Optional[AssetSnapshot]:
        """Detect which asset a market tracks and return its snapshot.

        Searches question, title, description, and slug for BTC/ETH/SOL keywords.
        Returns None if asset is not recognised or snapshot is unavailable.
        """
        text = " ".join(
            str(market.get(k) or "")
            for k in ("question", "title", "description", "slug")
        ).lower()

        for asset, keywords in _ASSET_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return self.assets.get(asset)

        return None


# ── Signal computation ──────────────────────────────────────────────────────────

def compute_binance_signal(
    market: Dict[str, Any],
    snapshot: BinanceSnapshot,
    cfg: SignalConfig,
) -> Optional[float]:
    """Compute the Binance-derived probability signal for a market.

    Formula:
        z         = recent_return / max(price_volatility, 1e-8)
        p_signal  = 0.5 + 0.5 * tanh(z * 0.5)
        p_signal  = clamp(p_signal, signal_min, signal_max)

    Returns None if:
    - The market's underlying asset cannot be detected from its text.
    - The asset snapshot is missing (Binance fetch failed for that asset).
    - recent_return is None (not enough kline data).
    """
    asset_snap = snapshot.get_asset_snapshot(market)
    if asset_snap is None:
        return None

    ret = asset_snap.recent_return
    if ret is None:
        return None

    vol = max(asset_snap.price_volatility or 1e-8, 1e-8)
    z = ret / vol
    p_signal = 0.5 + 0.5 * math.tanh(z * 0.5)

    return max(cfg.signal_min, min(cfg.signal_max, p_signal))


def blend_p_hat(
    p_internal: float,
    p_binance: Optional[float],
    cfg: SignalConfig,
) -> float:
    """Blend internal p_hat with the Binance signal.

    If p_binance is None (Binance unavailable or asset not detected),
    returns p_internal unchanged — the internal model is always authoritative.
    """
    if p_binance is None:
        return p_internal
    return cfg.binance_blend_weight * p_binance + (1.0 - cfg.binance_blend_weight) * p_internal
