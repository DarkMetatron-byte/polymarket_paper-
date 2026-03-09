# polymarket-engine (polymarket_paper export)

This repository contains a clean, transparent export of the original OpenClaw workspace project **`polymarket_paper/`**.

## What it does

- **Scanner**: discovers active Polymarket crypto *Up/Down* markets via the Gamma API.
- **Trader**: paper-trades only (no real orders) using a simple mean-reversion strategy.
- **Dashboard**: writes an `dashboard.html` snapshot.

## Key components

- `app/scanner.py`  market discovery (copied from `polymarket_paper/discover_markets.py`)
- `app/trader.py`  paper trader runner (copied from `polymarket_paper/trader.py`)
- `app/polymarket_client.py`  Gamma API client + helpers

## Run

From repo root:

```bash
python app/main.py
```

Or run individually:

```bash
python app/scanner.py
python app/trader.py
```

## Transparency

- Export inventory: `INVENTORY.md`
- Original files + runtime artifacts preserved under: `legacy/polymarket_paper/`
