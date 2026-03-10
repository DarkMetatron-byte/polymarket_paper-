# Config Overview

Runtime is mostly environment-variable driven.

## Core runtime

- `PM_STATE_PATH`
- Purpose: path for paper state JSON (positions/trades/metrics)
- Example: `/data/polymarket/paper_state.json`
- Default: `paper_state.json` in repo root

- `PM_DASHBOARD_PATH`
- Purpose: output path for dashboard HTML
- Example: `/data/polymarket/dashboard.html`
- Default: `dashboard.html` in repo root

- `TRADER_PRESET`
- Purpose: select threshold bundle for trader behavior
- Allowed: `conservative`, `normal`, `aggressive`
- Default: `normal`

## Telegram

- `TELEGRAM_BOT_TOKEN`
- Purpose: bot auth token for Telegram sendMessage API
- Example: `123456:ABCDEF...`
- Default if unset: Telegram send command fails unless `--bot-token` is passed

- `TELEGRAM_CHAT_ID`
- Purpose: target chat/channel/user id for status/alerts
- Example: `-1001234567890` (channel), `123456789` (user)
- Default if unset: Telegram send command fails unless `--chat-id` is passed

## Service loop / shell knobs

- `PYTHON_BIN`
- Purpose: explicit Python executable for run scripts
- Example: `/opt/polymarket/.venv/bin/python`
- Default: `.venv/bin/python` fallback to `python3`

- `INTERVAL_SECONDS`
- Purpose: loop interval for aligned service scripts
- Example: `900` (15 minutes), `3600` (hourly)
- Default: `900`

- `IMMEDIATE_FIRST_RUN`
- Purpose: run one immediate cycle before first aligned slot
- Allowed: `0` or `1`
- Default: `0`

## CLI-only equivalents (selected)

- `scripts/status_report.py --state-path --dashboard-path --format`
- `scripts/telegram_message.py --bot-token --chat-id --state-path --dashboard-path --dry-run --text`

## Notes

- No variable enables real trading in this repository.
- Current implementation remains paper-trading only.
