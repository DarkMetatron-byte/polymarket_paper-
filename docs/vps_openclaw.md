# VPS + OpenClaw Deployment

This runbook targets a Linux VPS where OpenClaw starts and supervises this repo.

## 1) Prepare environment

```bash
cd /path/to/polymarket_paper-
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 2) One-shot smoke test

```bash
.venv/bin/python app/main.py
```

Expected artifacts in repo root:

- `paper_state.json`
- `markets_cache.json`
- `dashboard.html`

## 3) Long-running process (simple)

```bash
bash scripts/run_server.sh
```

This runs one full cycle every 15 minutes.

## 4) OpenClaw control pattern

Use OpenClaw to run one of these commands in the repo cwd:

- one-shot cycle: `bash scripts/run_local.sh`
- continuous service: `bash scripts/run_server.sh`

If OpenClaw already uses Supervisor/systemd, wire it to `bash scripts/run_server.sh`.

## 5) Optional env vars

```bash
export PM_STATE_PATH=/data/polymarket/paper_state.json
export PM_DASHBOARD_PATH=/data/polymarket/dashboard.html
```

## 6) Operations notes

- paper-trading only (no real order placement)
- network access to `https://gamma-api.polymarket.com` is required
- keep process single-instance to avoid concurrent state writes

## 7) Status/report command for OpenClaw

```bash
python scripts/status_report.py
python scripts/status_report.py --format json
```

If runtime files are outside repo root:

```bash
python scripts/status_report.py --state-path /data/polymarket/paper_state.json --dashboard-path /data/polymarket/dashboard.html
```

## 8) Telegram push command

Set credentials in shell/session:

```bash
export TELEGRAM_BOT_TOKEN=<your_bot_token>
export TELEGRAM_CHAT_ID=<your_chat_id>
```

Send one status update:

```bash
python scripts/telegram_message.py
```

Dry run (print only):

```bash
python scripts/telegram_message.py --dry-run
```

## 9) Combined OpenClaw task (run + notify + alert)

Recommended single command:

```bash
bash scripts/run_openclaw_service.sh
```

Behavior:

- aligned quarter-hour slots (`:00`, `:15`, `:30`, `:45`)
- executes `app/main.py`
- sends status to Telegram on success
- sends ALERT text to Telegram on cycle failure

Optional env vars:

```bash
export INTERVAL_SECONDS=900
export IMMEDIATE_FIRST_RUN=1
```

## 10) Trader preset selection

Set before starting service:

```bash
export TRADER_PRESET=normal
```

Options: `conservative`, `normal`, `aggressive`.
See `docs/parameter_map.md`.

## 11) Learning highlights in reports

`status_report.py` and `telegram_message.py` include adaptive-learning highlights (`Top setups` / `Flop setups`) from `setup_table` when available.
