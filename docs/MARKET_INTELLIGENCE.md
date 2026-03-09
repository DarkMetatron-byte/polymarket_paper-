# Market Intelligence Layer

## Purpose

The Market Intelligence (MI) layer classifies each market's **structural quality** before it reaches the edge/mispricing model. Its goal is to filter out markets that are structurally bad trading candidates — regardless of what the price model says.

This prevents the system from placing trades on:
- Illiquid markets with wide spreads
- Markets about to resolve (hours away)
- Very distant futures markets (low activity, high uncertainty)
- Markets near certainty (price close to 0 or 1, little edge to capture)

---

## Module

`app/market_intelligence.py`

---

## Scoring Components

Each component returns a score in **[0.0, 1.0]**. When the required data is missing, the component returns **0.5** (neutral) to avoid blocking markets due to missing API fields.

### 1. `spread_score`

Based on the bid-ask spread (`bestAsk - bestBid`).

| Spread | Score |
|--------|-------|
| ≤ 0.005 | 1.0 (excellent) |
| 0.005 – 0.030 | Linear decay |
| ≥ 0.030 | 0.0 (too wide) |
| Missing | 0.5 (neutral) |

### 2. `liquidity_score`

Based on `min(bestBidSize, bestAskSize)` in USD notional.

| Min book size | Score |
|---------------|-------|
| ≥ $200 | 1.0 |
| $10 – $200 | Linear |
| ≤ $10 | 0.0 |
| Missing | 0.5 (neutral) |

### 3. `time_to_expiry_score`

Based on days until market resolution (`endDate`).

| Days remaining | Score |
|----------------|-------|
| < 1 day | 0.0 (imminent resolution) |
| 1 – 3 days | 0.0 → 0.30 (linear) |
| 3 – 7 days | 0.30 → 1.0 (linear) |
| **7 – 30 days** | **1.0 (sweet spot)** |
| 30 – 90 days | 1.0 → 0.50 (linear) |
| > 90 days | 0.30 (too distant) |
| Missing | 0.5 (neutral) |

### 4. `price_extreme_penalty`

Penalises markets where the mid-price is close to 0 or 1, where there is little room for mean reversion.

| Mid-price | Penalty score |
|-----------|---------------|
| ≤ 0.08 or ≥ 0.92 | 0.0 (hard penalty) |
| 0.08 – 0.15 or 0.85 – 0.92 | 0.0 → 1.0 (soft zone) |
| 0.15 – 0.85 | 1.0 (no penalty) |

---

## Combined Score

```
market_quality_score = (
    spread_score        × 0.30
  + liquidity_score     × 0.25
  + time_to_expiry_score × 0.25
  + price_extreme_penalty × 0.20
) × 100
```

Result is in **[0, 100]**.

---

## Classification Tiers

| Score | Classification | Trading pipeline |
|-------|---------------|-----------------|
| ≥ 75  | `HIGH_QUALITY` | ✅ Allowed |
| ≥ 50  | `NORMAL`       | ✅ Allowed |
| ≥ 30  | `LOW_QUALITY`  | ❌ Blocked |
| < 30  | `AVOID`        | ❌ Blocked |

Only `HIGH_QUALITY` and `NORMAL` markets proceed to edge evaluation and entry decisions.

**Exit rules are never blocked by MI** — open positions can always be closed regardless of classification.

---

## Configuration

All thresholds are in `MarketIntelConfig` (dataclass, frozen).

```python
from market_intelligence import MarketIntelConfig

# Use defaults
mi_cfg = MarketIntelConfig()

# Or override specific thresholds
mi_cfg = MarketIntelConfig(
    spread_excellent=0.003,    # tighter spread requirement
    liquidity_excellent=500.0, # require deeper book
    threshold_normal=60.0,     # raise the bar for NORMAL tier
)
```

The active config in `trader.py` is `MI_CFG = MarketIntelConfig()` (default values).

---

## Integration in `trader.py`

```
For each market in the candidate list:
  1. compute_market_intel(market, mid_price=price, mi_cfg=MI_CFG)
     → MarketIntelResult(liquidity_score, spread_score, time_to_expiry_score,
                         price_extreme_penalty, market_quality_score, classification)

  2. mi_stats[classification] += 1   # tracked for status report + dashboard

  3. EXIT RULES: always evaluated (MI does NOT block exits)

  4. ENTRY GATE:
     if not mi.is_tradeable(): skip   ← LOW_QUALITY and AVOID are blocked here

  5. Within tradeable entries: quality = mi.market_quality_score
     Secondary check: if quality < MIN_MARKET_QUALITY_SCORE (60): skip
```

### Status report (stdout)
```
[2026-03-09T12:00:00Z] trader: done market_scan(seen=2000, kept=45)
    positions=2 trades=37 mi=[AVOID=120 HIGH_QUALITY=45 LOW_QUALITY=310 NORMAL=82]
```

---

## Dashboard

The HTML dashboard (`dashboard.html`) shows MI data in three places:

1. **"Market Intelligence — last run classification counts"** section:
   Colour-coded badges showing how many markets fell into each tier in the last cycle.

2. **Open positions table**:
   `MI Score` (0–100) and `MI Class` badge columns.

3. **"Market Intelligence — trades by classification"** table:
   Historical trade performance broken down by entry MI classification (N, Win%, Avg P/L).

4. **Recent trades table**:
   `MI Class` badge column.

### Badge colours

| Classification | Colour |
|---------------|--------|
| `HIGH_QUALITY` | Green |
| `NORMAL` | Blue |
| `LOW_QUALITY` | Yellow/Amber |
| `AVOID` | Red |

---

## Tuning Guide

| Symptom | Likely cause | Suggested fix |
|---------|-------------|---------------|
| Too few tradeable markets | Thresholds too strict | Lower `threshold_normal` (e.g. 45) |
| Trades on illiquid markets | Liquidity score not filtering | Raise `liquidity_min` or `liquidity_excellent` |
| Trades too close to expiry | Expiry score insufficient | Raise `expiry_min_days` or adjust `threshold_high` |
| Trades on extreme prices | Price penalty not catching | Lower `price_penalty_lo` / raise `price_penalty_hi` |

All changes are local to `MI_CFG = MarketIntelConfig(...)` in `trader.py`.
