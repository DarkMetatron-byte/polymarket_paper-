# System Flow

Operations-focused view of one runtime cycle.

## One cycle

1. `app/main.py` starts a cycle.
2. `app/scanner.py` refreshes `trading_universe` and writes `markets_cache.json`.
3. `app/trader.py` loads `paper_state.json`.
4. Trader refreshes `observed_markets` snapshot via `app/market_scan.py` for dashboard visibility.
5. Trader reads `trading_universe` from cache (fallback: live discovery).
6. Trader evaluates filters and derives `tradeable_markets` candidates.
7. Trader applies entry/exit decisions (paper only) and updates state.
8. Trader writes `dashboard.html` and saves updated `paper_state.json`.
9. Service scripts call status/telegram scripts for updates/alerts.

## Read/write map

Reads:
- `markets_cache.json` (if present)
- `paper_state.json` (if present)

Writes:
- `markets_cache.json`
- `paper_state.json`
- `dashboard.html`

## Decision points

- market universe selection: `app/polymarket_client.py`
- trade filters + scoring + exits: `app/trader.py`
- broad observed scan for visibility: `app/market_scan.py`
- adaptive setup weighting/gating: `app/trader.py`

## Status and alerts

- status summary: `scripts/status_report.py`
- Telegram push: `scripts/telegram_message.py`
- scheduled loop and alert handling: `scripts/run_openclaw_service.sh`
