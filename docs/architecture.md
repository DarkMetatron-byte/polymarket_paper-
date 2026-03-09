# Architecture (export note)

This repo is an export of the original OpenClaw workspace folder `polymarket_paper/`.

## Runtime flow

1. **Scanner** discovers markets using Polymarket Gamma API.
2. **Trader** loads/updates a local JSON state file and applies a mean-reversion strategy.
3. **Dashboard** is written as a simple HTML file.

## Source mapping

- `app/scanner.py` was copied from `polymarket_paper/discover_markets.py`
- `app/trader.py` was copied from `polymarket_paper/trader.py`
- `app/polymarket_client.py` was copied from `polymarket_paper/polymarket_client.py`

All original files (including logs/state/caches) are preserved under `legacy/polymarket_paper/`.
