# Market Universe

This project uses three explicit market-set terms:

- `observed_markets`: broader dashboard/watchlist scan from Gamma
- `trading_universe`: active Polymarket crypto "Up or Down" markets
- `tradeable_markets`: subset of `trading_universe` that passes all trader filters

## observed_markets

Purpose:
- visibility and discovery
- operational transparency in dashboard/reporting

Definition:
- active markets from Gamma scan
- not resolved
- end date within configured horizon
- spread available and below configured spread threshold

Defined in:
- `app/market_scan.py`
- consumed by `app/trader.py` dashboard state (`state["market_scan"]`)

## trading_universe

Purpose:
- candidate set for paper-trading logic

Definition:
- active crypto "Up or Down" markets discovered via text heuristics
- keyword matching includes e.g. `up/down`, `bitcoin/btc`, `ethereum/eth`, `solana/sol`

Defined in:
- `app/polymarket_client.py` (`discover_active_crypto_updown_markets`)
- materialized by scanner cache in `markets_cache.json`

## tradeable_markets

Purpose:
- entries are considered only from this filtered subset

Definition:
- markets in `trading_universe` that pass trader checks such as:
- spread filter
- liquidity filter
- price-band filter
- quality-score threshold
- edge threshold
- cooldown guard
- circuit-breaker guard
- adaptive-learning gate (when enough sample exists)

Defined in:
- `app/trader.py`

## Reduction chain

```text
observed_markets -> trading_universe -> tradeable_markets
```

Practical note:
- `observed_markets` and `trading_universe` come from different discovery paths on purpose.
- This keeps dashboard visibility broad while keeping paper-trading focused.
