from __future__ import annotations

"""Send status report messages to Telegram.

Defaults:
- Builds message from scripts/status_report.py data model
- Sends plain text via Telegram Bot API sendMessage

Environment variables:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict


THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from status_report import compute_report, load_json, render_text  # noqa: E402


def build_message(
    *,
    state_path: str,
    scan_cache_path: str,
    dashboard_path: str,
) -> str:
    state = load_json(state_path)
    scan_cache = load_json(scan_cache_path)

    if state is None:
        cache_count = None
        if isinstance(scan_cache, dict) and scan_cache.get("count") is not None:
            try:
                cache_count = int(float(scan_cache.get("count")))
            except Exception:
                cache_count = None

        return (
            "Polymarket Bot Status\n"
            "Health: BOOTSTRAPPING\n"
            f"Alerts: state file not found or invalid JSON: {state_path}\n"
            "Runtime: state_age=Nonem, open_positions=0, cb_losses=0\n"
            "Performance: realized_pnl=0.0, win_rate=0.0% (0 trades)\n"
            "Recent PnL: 24h=0.0 (0 trades), today_utc=0.0 (0 trades)\n"
            f"Market scan: seen=0, kept=0, cache_count={cache_count}\n"
            f"Dashboard: exists={os.path.exists(dashboard_path)}, age=Nonem\n"
            "Learning table: setups=0, sampled(>= 8 trades)=0"
        )

    report = compute_report(state, scan_cache, dashboard_path)
    return render_text(report)


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not parsed.get("ok"):
        raise RuntimeError(f"Telegram API error: {raw}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Polymarket status to Telegram")
    parser.add_argument("--bot-token", default=os.environ.get("TELEGRAM_BOT_TOKEN"))
    parser.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID"))
    parser.add_argument("--state-path", default=os.environ.get("PM_STATE_PATH", "paper_state.json"))
    parser.add_argument("--scan-cache-path", default="markets_cache.json")
    parser.add_argument("--dashboard-path", default=os.environ.get("PM_DASHBOARD_PATH", "dashboard.html"))
    parser.add_argument("--text", help="Send this explicit message instead of auto status report")
    parser.add_argument("--dry-run", action="store_true", help="Print message and do not send")
    args = parser.parse_args()

    if args.text:
        message = args.text
    else:
        message = build_message(
            state_path=args.state_path,
            scan_cache_path=args.scan_cache_path,
            dashboard_path=args.dashboard_path,
        )

    # Telegram max text length is 4096 chars. Keep latest context by truncating head.
    if len(message) > 4096:
        message = "[truncated]\n" + message[-4084:]

    if args.dry_run:
        print(message)
        return 0

    if not args.bot_token:
        raise SystemExit("missing bot token. Use --bot-token or TELEGRAM_BOT_TOKEN")
    if not args.chat_id:
        raise SystemExit("missing chat id. Use --chat-id or TELEGRAM_CHAT_ID")

    send_telegram_message(args.bot_token, args.chat_id, message)
    print("telegram message sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


