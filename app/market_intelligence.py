"""Market Intelligence Layer.

Classifies markets before edge evaluation to avoid structurally bad markets.
All scoring is purely structural — independent of the mispricing/edge model.

Scoring components (each 0..1):
  - spread_score           tight spread  = 1.0,  wide spread  = 0.0
  - liquidity_score        deep book     = 1.0,  thin book    = 0.0
  - time_to_expiry_score   7-30 days     = 1.0,  too soon/far = 0.0
  - price_extreme_penalty  mid-range     = 1.0,  near 0 or 1  = 0.0

Combined into market_quality_score (0..100) and classified as:
  HIGH_QUALITY  >= 75   green light
  NORMAL        >= 50   green light
  LOW_QUALITY   >= 30   skip entry
  AVOID         <  30   hard skip

Only HIGH_QUALITY and NORMAL markets reach the trading pipeline.

Usage:
    from market_intelligence import DEFAULT_CONFIG, compute_market_intel

    result = compute_market_intel(market_dict, mid_price=0.45)
    if result.is_tradeable():
        ...  # proceed to edge evaluation
    print(result.to_dict())
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


# ── Classification tiers ───────────────────────────────────────────────────────

class MarketClass(str, Enum):
    """Market quality classification tiers."""
    HIGH_QUALITY = "HIGH_QUALITY"  # score >= threshold_high  → green light
    NORMAL       = "NORMAL"        # score >= threshold_normal → green light
    LOW_QUALITY  = "LOW_QUALITY"   # score >= threshold_low    → skip entry
    AVOID        = "AVOID"         # score <  threshold_low    → hard skip


# Markets allowed to enter the trading pipeline.
TRADEABLE_CLASSES: frozenset[MarketClass] = frozenset({
    MarketClass.HIGH_QUALITY,
    MarketClass.NORMAL,
})

# Human-readable labels for the dashboard.
CLASS_LABELS: Dict[str, str] = {
    MarketClass.HIGH_QUALITY: "HIGH",
    MarketClass.NORMAL:       "NORMAL",
    MarketClass.LOW_QUALITY:  "LOW",
    MarketClass.AVOID:        "AVOID",
}


# ── Configuration ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketIntelConfig:
    """Tunable thresholds for the market intelligence scoring system."""

    # Spread thresholds (ask - bid, expressed as a fraction 0..1)
    spread_excellent: float = 0.005   # <= this  → spread_score = 1.0
    spread_max: float = 0.030         # >= this  → spread_score = 0.0  (linear between)

    # Liquidity thresholds (min of bestBidSize, bestAskSize in USD notional)
    liquidity_excellent: float = 200.0   # >= this → liq_score = 1.0
    liquidity_min: float = 10.0          # <= this → liq_score = 0.0  (linear between)

    # Time-to-expiry thresholds (days until resolution)
    expiry_min_days: float = 1.0         # < 1 day   → score 0.0  (imminent resolution)
    expiry_short_days: float = 3.0       # 1-3 days  → score 0.0 → 0.30
    expiry_ideal_low: float = 7.0        # 3-7 days  → score 0.30 → 1.0
    expiry_ideal_high: float = 30.0      # 7-30 days → score 1.0  (sweet spot)
    expiry_long_days: float = 90.0       # 30-90 days → score 1.0 → 0.50
                                         # > 90 days  → score 0.30

    # Price extreme thresholds (mid_price as fraction 0..1)
    price_extreme_lo: float = 0.08       # <= this        → penalty = 0.0 (hard)
    price_extreme_hi: float = 0.92       # >= this        → penalty = 0.0 (hard)
    price_penalty_lo: float = 0.15       # soft-zone lower boundary
    price_penalty_hi: float = 0.85       # soft-zone upper boundary

    # Component weights — must sum to 1.0 (enforced at runtime)
    weight_spread:    float = 0.30
    weight_liquidity: float = 0.25
    weight_expiry:    float = 0.25
    weight_price:     float = 0.20

    # Classification thresholds (market_quality_score is 0..100)
    threshold_high:   float = 75.0
    threshold_normal: float = 50.0
    threshold_low:    float = 30.0


# Singleton default config — import and use directly.
DEFAULT_CONFIG = MarketIntelConfig()


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class MarketIntelResult:
    """Full result of a single market intelligence evaluation."""
    liquidity_score:      float        # 0..1
    spread_score:         float        # 0..1
    time_to_expiry_score: float        # 0..1
    price_extreme_penalty: float       # 0..1  (1 = no penalty)
    market_quality_score: float        # 0..100 (weighted combination)
    classification:       MarketClass

    def is_tradeable(self) -> bool:
        """True if this market is allowed to enter the trading pipeline."""
        return self.classification in TRADEABLE_CLASSES

    def to_dict(self) -> Dict[str, Any]:
        """Serialisable representation for state files and dashboard."""
        return {
            "liquidity_score":       round(self.liquidity_score, 4),
            "spread_score":          round(self.spread_score, 4),
            "time_to_expiry_score":  round(self.time_to_expiry_score, 4),
            "price_extreme_penalty": round(self.price_extreme_penalty, 4),
            "market_quality_score":  round(self.market_quality_score, 2),
            "classification":        self.classification.value,
        }


# ── Internal helpers ───────────────────────────────────────────────────────────

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _parse_float(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def _parse_end_epoch(m: Dict[str, Any]) -> Optional[float]:
    """Parse market end date → UTC epoch seconds. Returns None if unavailable."""
    s = m.get("endDate") or m.get("end_date") or m.get("end")
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
        # Bare datetime or offset — take first 19 chars as UTC (best effort)
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return None


# ── Individual scoring functions ───────────────────────────────────────────────

def compute_spread_score(market: Dict[str, Any], cfg: MarketIntelConfig) -> float:
    """Score based on bid-ask spread width.

    Returns 0.5 (neutral) when spread cannot be computed.
    """
    bb = _parse_float(market.get("bestBid"))
    ba = _parse_float(market.get("bestAsk"))
    if bb is None or ba is None or bb < 0 or ba < 0 or bb > ba:
        return 0.5  # unknown → neutral

    spread = ba - bb
    if spread <= cfg.spread_excellent:
        return 1.0
    if spread >= cfg.spread_max:
        return 0.0
    # Linear decay from excellent → max
    return _clamp01(
        1.0 - (spread - cfg.spread_excellent) / (cfg.spread_max - cfg.spread_excellent)
    )


def compute_liquidity_score(market: Dict[str, Any], cfg: MarketIntelConfig) -> float:
    """Score based on top-of-book order size.

    Uses min(bestBidSize, bestAskSize) as effective liquidity proxy.
    Returns 0.5 (neutral) when sizes are unavailable.
    """
    bbs = _parse_float(market.get("bestBidSize"))
    bas = _parse_float(market.get("bestAskSize"))
    if bbs is None or bas is None:
        return 0.5  # unknown → neutral

    liq = min(bbs, bas)
    if liq <= 0:
        return 0.0
    if liq >= cfg.liquidity_excellent:
        return 1.0
    if liq <= cfg.liquidity_min:
        return 0.0
    return _clamp01(
        (liq - cfg.liquidity_min) / (cfg.liquidity_excellent - cfg.liquidity_min)
    )


def compute_time_to_expiry_score(market: Dict[str, Any], cfg: MarketIntelConfig) -> float:
    """Score based on days remaining until market resolution.

    Sweet spot: 7–30 days (score 1.0).
    Imminent (<1 day) and very distant (>90 days) markets score poorly.
    Returns 0.5 (neutral) when end date is unavailable.
    """
    end_epoch = _parse_end_epoch(market)
    if end_epoch is None:
        return 0.5  # unknown → neutral

    days = (end_epoch - time.time()) / 86_400.0

    if days < cfg.expiry_min_days:
        return 0.0  # imminent resolution

    if days < cfg.expiry_short_days:
        # 1 → 3 days: 0.0 → 0.30
        return _clamp01(
            0.30 * (days - cfg.expiry_min_days)
            / (cfg.expiry_short_days - cfg.expiry_min_days)
        )

    if days < cfg.expiry_ideal_low:
        # 3 → 7 days: 0.30 → 1.0
        return _clamp01(
            0.30 + 0.70 * (days - cfg.expiry_short_days)
            / (cfg.expiry_ideal_low - cfg.expiry_short_days)
        )

    if days <= cfg.expiry_ideal_high:
        return 1.0  # sweet spot: 7–30 days

    if days <= cfg.expiry_long_days:
        # 30 → 90 days: 1.0 → 0.50
        return _clamp01(
            1.0 - 0.50 * (days - cfg.expiry_ideal_high)
            / (cfg.expiry_long_days - cfg.expiry_ideal_high)
        )

    # > 90 days: fixed low score
    return 0.30


def compute_price_extreme_penalty(mid_price: float, cfg: MarketIntelConfig) -> float:
    """Penalty for prices near 0 or 1 (near-resolved markets).

    Returns 1.0 = no penalty (healthy mid-range price).
    Returns 0.0 = maximum penalty (price at extreme end).

    Zones (symmetric, described for the lower end):
      [0,               price_extreme_lo]  → 0.0  hard penalty
      [price_extreme_lo, price_penalty_lo] → 0.0 → 1.0  linear
      [price_penalty_lo, price_penalty_hi] → 1.0  no penalty
      [price_penalty_hi, price_extreme_hi] → 1.0 → 0.0  linear
      [price_extreme_hi, 1.0]              → 0.0  hard penalty
    """
    p = _clamp01(float(mid_price))

    # Hard extreme zones
    if p <= cfg.price_extreme_lo or p >= cfg.price_extreme_hi:
        return 0.0

    # Soft penalty — lower side
    if p < cfg.price_penalty_lo:
        return _clamp01(
            (p - cfg.price_extreme_lo) / (cfg.price_penalty_lo - cfg.price_extreme_lo)
        )

    # Soft penalty — upper side
    if p > cfg.price_penalty_hi:
        return _clamp01(
            (cfg.price_extreme_hi - p) / (cfg.price_extreme_hi - cfg.price_penalty_hi)
        )

    # No-penalty zone
    return 1.0


# ── Combined evaluation ────────────────────────────────────────────────────────

def compute_market_intel(
    market: Dict[str, Any],
    *,
    mid_price: float,
    mi_cfg: MarketIntelConfig = DEFAULT_CONFIG,
) -> MarketIntelResult:
    """Compute all MI component scores and return a MarketIntelResult.

    This is the main entry point for the market intelligence layer.
    Fast, pure, and side-effect free — safe to call in a tight loop.

    Args:
        market:    Raw market dict from Polymarket Gamma API.
        mid_price: Current mid-price of the YES outcome (0..1).
        mi_cfg:    Optional config override (defaults to DEFAULT_CONFIG).

    Returns:
        MarketIntelResult with scores, quality score, and classification.
    """
    liq_score  = compute_liquidity_score(market, mi_cfg)
    spread_sc  = compute_spread_score(market, mi_cfg)
    expiry_sc  = compute_time_to_expiry_score(market, mi_cfg)
    price_pen  = compute_price_extreme_penalty(mid_price, mi_cfg)

    total_weight = (
        mi_cfg.weight_spread
        + mi_cfg.weight_liquidity
        + mi_cfg.weight_expiry
        + mi_cfg.weight_price
    ) or 1.0  # guard against misconfigured weights

    score01 = (
        spread_sc * mi_cfg.weight_spread
        + liq_score * mi_cfg.weight_liquidity
        + expiry_sc * mi_cfg.weight_expiry
        + price_pen * mi_cfg.weight_price
    ) / total_weight

    quality = _clamp01(score01) * 100.0

    # Classify
    if quality >= mi_cfg.threshold_high:
        cls = MarketClass.HIGH_QUALITY
    elif quality >= mi_cfg.threshold_normal:
        cls = MarketClass.NORMAL
    elif quality >= mi_cfg.threshold_low:
        cls = MarketClass.LOW_QUALITY
    else:
        cls = MarketClass.AVOID

    return MarketIntelResult(
        liquidity_score=liq_score,
        spread_score=spread_sc,
        time_to_expiry_score=expiry_sc,
        price_extreme_penalty=price_pen,
        market_quality_score=quality,
        classification=cls,
    )
