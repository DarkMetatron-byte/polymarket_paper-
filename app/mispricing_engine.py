"""Mispricing engine v1 (paper trading).

Goal: compute market probability p_mkt (from midprice) and an estimated probability p_hat
(from internal price history), then edge = p_hat - p_mkt.

No external dependencies.

This is intentionally simple and modular.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


def clamp(x: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, x))


def sma(values: List[float]) -> float:
    return sum(values) / len(values)


def get_history_prices(state: Dict[str, Any], market_id: str) -> List[float]:
    hist = state.get("price_history", {}).get(market_id, [])
    if not isinstance(hist, list):
        return []
    out: List[float] = []
    for x in hist:
        if isinstance(x, dict) and "p" in x:
            try:
                out.append(float(x["p"]))
            except Exception:
                pass
    return out


@dataclass
class MispricingConfig:
    sma_window: int = 8
    momentum_k: float = 0.5
    edge_enter: float = 0.05
    edge_exit: float = 0.01
    max_spread: float = 0.03

    # Warmstart: allow p_hat estimation with fewer than sma_window points
    warmstart_min_points: int = 3
    warmstart_edge_scale: float = 1.5  # require moderately larger edge early (edge_enter * scale)


def market_probability_from_midprice(price: float) -> float:
    return clamp(float(price), 0.0, 1.0)


def estimate_probability_v1(
    state: Dict[str, Any],
    market_id: str,
    p_mkt: float,
    cfg: MispricingConfig,
) -> Optional[float]:
    """Internal-only model.

    Base model:
      p_hat = SMA(n) + k*(last - SMA(n))

    Warmstart:
      If not enough points for SMA(n), but we have >= warmstart_min_points,
      use SMA(m) where m=len(history) and keep the same form.

    Returns None if not enough data.
    """
    ps = get_history_prices(state, market_id)

    if len(ps) >= cfg.sma_window:
        window = cfg.sma_window
    elif len(ps) >= cfg.warmstart_min_points:
        window = len(ps)
    else:
        return None

    s = sma(ps[-window:])
    last = ps[-1]
    p_hat = s + cfg.momentum_k * (last - s)
    return clamp(p_hat)


def compute_edge(p_hat: float, p_mkt: float) -> float:
    return float(p_hat) - float(p_mkt)


def spread_ok(market: Dict[str, Any], cfg: MispricingConfig) -> bool:
    bb = market.get("bestBid")
    ba = market.get("bestAsk")
    try:
        if bb is None or ba is None:
            return True  # can't check; don't block
        bb = float(bb)
        ba = float(ba)
        if bb <= 0 or ba <= 0:
            return True
        return (ba - bb) <= cfg.max_spread
    except Exception:
        return True


def entry_threshold(cfg: MispricingConfig, *, points: int) -> float:
    th = cfg.edge_enter
    if points < cfg.sma_window:
        th = cfg.edge_enter * cfg.warmstart_edge_scale
    return th


def entry_signal(edge: float, cfg: MispricingConfig, *, points: int) -> Optional[str]:
    threshold = entry_threshold(cfg, points=points)
    if edge >= threshold:
        return "BUY_YES"
    if edge <= -threshold:
        return "BUY_NO"
    return None


def exit_signal(edge: float, cfg: MispricingConfig) -> bool:
    return abs(edge) <= cfg.edge_exit
