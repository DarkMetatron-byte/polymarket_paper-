# Trader Parameter Map

This file documents the key knobs in `app/trader.py` and how they influence behavior.

## Active preset switch

Set via env var:

```bash
export TRADER_PRESET=normal
```

Allowed values:

- `conservative`
- `normal` (default)
- `aggressive`

The preset modifies selected thresholds before each run.

## Core thresholds

- `EDGE_ENTER`: minimum expected edge required for a new position.
  Higher = fewer entries, usually cleaner setups.
- `EDGE_EXIT`: normal edge exit threshold for closing positions.
  Higher = exits sooner.
- `EDGE_ADVERSE_EXIT`: hard/adverse stop on edge deterioration.
  Less negative = quicker risk-off exit.
- `MAX_SPREAD`: maximum allowed bid/ask spread at entry and market scan filtering.
  Lower = better execution quality, fewer candidates.
- `MIN_BEST_SIZE`: minimum top-of-book size per side.
  Higher = stronger liquidity filter.
- `COOLDOWN_MINUTES`: re-entry cooldown after closing a position in the same market.
  Higher = less churn.
- `MAX_HOLD_MINUTES`: max position lifetime.
  Lower = faster turnover.
- `MIN_MARKET_QUALITY_SCORE`: minimum quality score [0..100] required for entry.
  Higher = stricter selection.

## Price and capital guards

- `MIN_PRICE` / `MAX_PRICE`: allowed entry price band for YES/NO instrument.
- `MAX_USD_PER_TRADE`: fixed notional per trade (currently `$10`).
- `MAX_CONSEC_LOSSES`: circuit breaker threshold (currently `3`).

## Preset values

### conservative

- `EDGE_ENTER=0.12`
- `EDGE_EXIT=0.015`
- `EDGE_ADVERSE_EXIT=-0.015`
- `MAX_SPREAD=0.02`
- `MIN_BEST_SIZE=80`
- `COOLDOWN_MINUTES=90`
- `MAX_HOLD_MINUTES=240`
- `MIN_MARKET_QUALITY_SCORE=70`

Use when you want fewer, higher-quality trades with lower churn.

### normal (default)

- `EDGE_ENTER=0.10`
- `EDGE_EXIT=0.01`
- `EDGE_ADVERSE_EXIT=-0.02`
- `MAX_SPREAD=0.03`
- `MIN_BEST_SIZE=50`
- `COOLDOWN_MINUTES=60`
- `MAX_HOLD_MINUTES=360`
- `MIN_MARKET_QUALITY_SCORE=60`

Balanced baseline.

### aggressive

- `EDGE_ENTER=0.08`
- `EDGE_EXIT=0.005`
- `EDGE_ADVERSE_EXIT=-0.03`
- `MAX_SPREAD=0.04`
- `MIN_BEST_SIZE=30`
- `COOLDOWN_MINUTES=30`
- `MAX_HOLD_MINUTES=480`
- `MIN_MARKET_QUALITY_SCORE=55`

Use when you want more trades and can tolerate noisier fills/signals.

## Operational recommendation

- Start with `normal` for at least 3-7 days.
- Compare `pnl_24h`, `win_rate`, and `spread_skips` from status reports.
- Change only one preset at a time and keep run cadence unchanged.

## Adaptive learning (Phase A+B)

The trader now keeps a setup table in state (`setup_table`) and uses it to adjust selection.

- Phase A (observe): every closed trade updates the setup row.
- Phase B (prioritize/gate): entry candidates are weighted or blocked by historical setup performance.

Setup key dimensions:

- active preset (`TRADER_PRESET`)
- side (`YES`/`NO`)
- spread bucket
- quality bucket

Learning knobs:

- `LEARNING_ENABLED`: turn adaptive logic on/off
- `LEARNING_MIN_TRADES`: minimum closed trades before a setup affects decisions
- `LEARNING_BLOCK_NON_POSITIVE`: if true, setups with avg PnL <= 0 are blocked once sample size is reached
- `LEARNING_PRIORITY_K`: scales priority boost/cut from avg PnL
- `LEARNING_MIN_FACTOR` / `LEARNING_MAX_FACTOR`: clamps for priority multiplier

Interpretation:

- before `LEARNING_MIN_TRADES`, setup factor is neutral (`1.0`)
- after enough data:
- if non-positive avg PnL and blocking enabled, setup is skipped
- otherwise candidate priority is multiplied by bounded factor
