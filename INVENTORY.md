# INVENTORY

This is a transparent export of the original OpenClaw workspace project `polymarket_paper/`.

## Current repo structure (clean)

- `app/main.py`  minimal entrypoint (scanner then trader)
- `app/scanner.py`  market discovery (copied from `polymarket_paper/discover_markets.py`)
- `app/trader.py`  paper trader runner (copied from `polymarket_paper/trader.py`)
- `app/polymarket_client.py`  Gamma API client + helpers
- `app/config.py`  config placeholder (new)

- `scripts/analyze_trades.py`  trade analysis script
- `scripts/run_server.sh`  original run script (copied from `run.sh`)
- `scripts/run_local.sh`  original supervisor start script (copied from `supervisor_start.sh`)

- `docs/architecture.md`  mapping + flow overview (new)
- `docs/strategy.md`  strategy summary (new)
- `docs/notes.md`  export notes (new)

- `tests/test_scanner.py`  placeholder test (new)
- `.codex/config.yaml`  Codex project hint (new)

- `requirements.txt`  minimal placeholder (new)
- `.gitignore`  excludes state/log/caches
- `README.md`  project overview + run instructions

## Preserved originals (legacy/)

Everything from the original `polymarket_paper/` folder (including logs/state/cache and skill markdowns) was copied under:

- `legacy/polymarket_paper/*`

Top-level legacy items:

- `legacy/polymarket-*.skill.md`  OpenClaw skill docs used in the workspace

Notably preserved runtime artifacts:

- `legacy/polymarket_paper/paper_state.json`  paper trading state + trade log
- `legacy/polymarket_paper/markets_cache.json`  cached markets
- `legacy/polymarket_paper/*.log`  runtime logs
- `legacy/polymarket_paper/dashboard.html`  generated dashboard
- `legacy/polymarket_paper/supervisord.*`  supervisor config/logs
