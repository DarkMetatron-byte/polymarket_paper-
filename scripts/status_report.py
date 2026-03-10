from __future__ import annotations

"""Compact operational status report for OpenClaw/Telegram.

Reads runtime artifacts and prints either:
- human-readable text report (default)
- machine-readable JSON (--format json)
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


def parse_utc(ts: str | None) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def age_minutes(ts: str | None, now: datetime) -> Optional[float]:
    dt = parse_utc(ts)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 60.0


def load_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def summarize_learning(state: Dict[str, Any], *, top_k: int = 3, min_trades: int = 8) -> Dict[str, Any]:
    table = state.get("setup_table")
    if not isinstance(table, dict):
        return {
            "setup_count": 0,
            "sampled_setup_count": 0,
            "min_trades_hint": min_trades,
            "top": [],
            "bottom": [],
        }

    rows: List[Dict[str, Any]] = []
    sampled_rows: List[Dict[str, Any]] = []

    for key, row in table.items():
        if not isinstance(row, dict):
            continue
        n = int(fnum(row.get("n"), 0.0))
        wins = int(fnum(row.get("wins"), 0.0))
        sum_pnl = fnum(row.get("sum_pnl"), 0.0)
        avg_pnl = fnum(row.get("avg_pnl"), 0.0)

        item = {
            "setup_key": str(key),
            "n": n,
            "wins": wins,
            "win_rate_pct": round((wins / n * 100.0), 2) if n else 0.0,
            "sum_pnl": round(sum_pnl, 4),
            "avg_pnl": round(avg_pnl, 4),
            "last_updated": row.get("last_updated"),
        }
        rows.append(item)
        if n >= min_trades:
            sampled_rows.append(item)

    scored = sampled_rows if sampled_rows else rows
    top = sorted(scored, key=lambda r: (r["avg_pnl"], r["sum_pnl"], r["n"]), reverse=True)[:top_k]
    bottom = sorted(scored, key=lambda r: (r["avg_pnl"], r["sum_pnl"], -r["n"]))[:top_k]

    return {
        "setup_count": len(rows),
        "sampled_setup_count": len(sampled_rows),
        "min_trades_hint": min_trades,
        "top": top,
        "bottom": bottom,
    }


def compute_report(state: Dict[str, Any], scan_cache: Optional[Dict[str, Any]], dashboard_path: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)

    trades = state.get("trades") if isinstance(state.get("trades"), list) else []
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}

    generated_at = state.get("generated_at") if isinstance(state.get("generated_at"), str) else None
    generated_age_min = age_minutes(generated_at, now)

    wins = sum(1 for t in trades if fnum((t or {}).get("pnl"), 0.0) > 0)
    total = len(trades)
    win_rate = (wins / total * 100.0) if total else 0.0

    realized_pnl = fnum(state.get("realized_pnl"), 0.0)
    consecutive_losses = int(fnum(state.get("consecutive_losses"), 0.0))
    circuit_breaker_tripped = consecutive_losses >= 3

    cutoff_24h = now - timedelta(hours=24)
    pnl_24h = 0.0
    trades_24h = 0
    last_exit_time = None
    last_exit_dt = None

    for t in trades:
        if not isinstance(t, dict):
            continue
        et = parse_utc(t.get("exit_time") if isinstance(t.get("exit_time"), str) else None)
        if et is None:
            continue
        if last_exit_dt is None or et > last_exit_dt:
            last_exit_dt = et
            last_exit_time = t.get("exit_time")
        if et >= cutoff_24h:
            trades_24h += 1
            pnl_24h += fnum(t.get("pnl"), 0.0)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    pnl_today = 0.0
    trades_today = 0
    for t in trades:
        if not isinstance(t, dict):
            continue
        et = parse_utc(t.get("exit_time") if isinstance(t.get("exit_time"), str) else None)
        if et is not None and et >= today_start:
            trades_today += 1
            pnl_today += fnum(t.get("pnl"), 0.0)

    market_scan = state.get("market_scan") if isinstance(state.get("market_scan"), dict) else {}
    trade_filters = state.get("trade_filter_stats") if isinstance(state.get("trade_filter_stats"), dict) else {}

    cache_generated_at = None
    cache_count = None
    if isinstance(scan_cache, dict):
        if isinstance(scan_cache.get("generated_at"), str):
            cache_generated_at = scan_cache.get("generated_at")
        if scan_cache.get("count") is not None:
            cache_count = int(fnum(scan_cache.get("count"), 0.0))

    dashboard_exists = os.path.exists(dashboard_path)
    dashboard_age_min = None
    if dashboard_exists:
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(dashboard_path), tz=timezone.utc)
            dashboard_age_min = (now - mtime).total_seconds() / 60.0
        except Exception:
            dashboard_age_min = None

    health = "OK"
    alerts: List[str] = []

    if generated_age_min is None:
        health = "WARN"
        alerts.append("state/generated_at missing")
    elif generated_age_min > 20:
        health = "WARN"
        alerts.append(f"state stale ({generated_age_min:.1f} min)")

    if circuit_breaker_tripped:
        health = "WARN" if health == "OK" else health
        alerts.append("circuit breaker tripped (>=3 consecutive losses)")

    if dashboard_exists and dashboard_age_min is not None and dashboard_age_min > 30:
        health = "WARN" if health == "OK" else health
        alerts.append(f"dashboard stale ({dashboard_age_min:.1f} min)")

    if isinstance(state.get("market_scan_error"), str) and state.get("market_scan_error"):
        health = "WARN" if health == "OK" else health
        alerts.append("market_scan_error present")

    recent_trades = []
    for t in list(trades)[-5:]:
        if not isinstance(t, dict):
            continue
        recent_trades.append(
            {
                "exit_time": t.get("exit_time"),
                "slug": t.get("slug"),
                "side": t.get("side"),
                "exit_reason": t.get("exit_reason"),
                "pnl": round(fnum(t.get("pnl"), 0.0), 4),
            }
        )

    learning = summarize_learning(state, top_k=3, min_trades=8)

    return {
        "generated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "health": health,
        "alerts": alerts,
        "state": {
            "generated_at": generated_at,
            "age_minutes": None if generated_age_min is None else round(generated_age_min, 2),
            "realized_pnl": round(realized_pnl, 4),
            "consecutive_losses": consecutive_losses,
            "circuit_breaker_tripped": circuit_breaker_tripped,
            "open_positions": len(positions),
            "closed_trades": total,
            "win_rate_pct": round(win_rate, 2),
            "last_trade_exit_time": last_exit_time,
            "pnl_24h": round(pnl_24h, 4),
            "trades_24h": trades_24h,
            "pnl_today_utc": round(pnl_today, 4),
            "trades_today_utc": trades_today,
        },
        "market_scan": {
            "seen": int(fnum(market_scan.get("total_seen"), 0.0)) if market_scan else 0,
            "kept": int(fnum(market_scan.get("total_kept"), 0.0)) if market_scan else 0,
            "generated_at": market_scan.get("generated_at") if isinstance(market_scan.get("generated_at"), str) else None,
            "cache_generated_at": cache_generated_at,
            "cache_count": cache_count,
            "error": state.get("market_scan_error") if isinstance(state.get("market_scan_error"), str) else None,
        },
        "trade_filters": {
            "observed_markets": int(fnum(trade_filters.get("observed_markets"), 0.0)),
            "trading_universe": int(fnum(trade_filters.get("trading_universe"), 0.0)),
            "tradeable_markets": int(fnum(trade_filters.get("tradeable_markets"), 0.0)),
            "blocked_reasons": trade_filters.get("blocked_reasons") if isinstance(trade_filters.get("blocked_reasons"), dict) else {},
        },
        "dashboard": {
            "path": dashboard_path,
            "exists": dashboard_exists,
            "age_minutes": None if dashboard_age_min is None else round(dashboard_age_min, 2),
        },
        "learning": learning,
        "recent_trades": recent_trades,
    }


def render_text(report: Dict[str, Any]) -> str:
    s = report["state"]
    m = report["market_scan"]
    tf = report.get("trade_filters") or {}
    d = report["dashboard"]
    l = report.get("learning") or {}

    lines = []
    lines.append("Polymarket Bot Status")
    lines.append(f"Health: {report['health']}")

    alerts = report.get("alerts") or []
    if alerts:
        lines.append("Alerts: " + "; ".join(str(a) for a in alerts))

    lines.append(
        "Runtime: "
        f"state_age={s.get('age_minutes')}m, "
        f"open_positions={s.get('open_positions')}, "
        f"cb_losses={s.get('consecutive_losses')}"
    )
    lines.append(
        "Performance: "
        f"realized_pnl={s.get('realized_pnl')}, "
        f"win_rate={s.get('win_rate_pct')}% ({int(s.get('closed_trades', 0) or 0)} trades)"
    )
    lines.append(
        "Recent PnL: "
        f"24h={s.get('pnl_24h')} ({s.get('trades_24h')} trades), "
        f"today_utc={s.get('pnl_today_utc')} ({s.get('trades_today_utc')} trades)"
    )
    lines.append(
        "Market scan: "
        f"seen={m.get('seen')}, kept={m.get('kept')}, cache_count={m.get('cache_count')}"
    )
    lines.append(
        "Market labels: "
        f"observed_markets={tf.get('observed_markets')}, "
        f"trading_universe={tf.get('trading_universe')}, "
        f"tradeable_markets={tf.get('tradeable_markets')}"
    )
    blocked = tf.get("blocked_reasons") or {}
    if blocked:
        btxt = ", ".join(f"{k}:{int(v)}" for k, v in sorted(blocked.items(), key=lambda kv: kv[0]))
        lines.append(f"Blocked reasons: {btxt}")

    lines.append(
        "Dashboard: "
        f"exists={d.get('exists')}, age={d.get('age_minutes')}m"
    )

    lines.append(
        "Learning table: "
        f"setups={int(l.get('setup_count', 0) or 0)}, "
        f"sampled(>= {int(l.get('min_trades_hint', 0) or 0)} trades)={int(l.get('sampled_setup_count', 0) or 0)}"
    )

    top = l.get("top") or []
    if top:
        lines.append("Top setups:")
        for r in top:
            lines.append(f"+ avg={r.get('avg_pnl')} n={r.get('n')} wr={r.get('win_rate_pct')}% | {r.get('setup_key')}")

    bottom = l.get("bottom") or []
    if bottom:
        lines.append("Flop setups:")
        for r in bottom:
            lines.append(f"- avg={r.get('avg_pnl')} n={r.get('n')} wr={r.get('win_rate_pct')}% | {r.get('setup_key')}")

    last_exit = s.get("last_trade_exit_time")
    if last_exit:
        lines.append(f"Last trade exit: {last_exit}")

    recent = report.get("recent_trades") or []
    if recent:
        lines.append("Recent trades:")
        for t in reversed(recent):
            lines.append(
                f"- {t.get('exit_time')} | {t.get('slug')} | {t.get('side')} | {t.get('exit_reason')} | pnl={t.get('pnl')}"
            )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Operational status report for Polymarket paper trader")
    parser.add_argument("--state-path", default=os.environ.get("PM_STATE_PATH", "paper_state.json"))
    parser.add_argument("--scan-cache-path", default="markets_cache.json")
    parser.add_argument("--dashboard-path", default=os.environ.get("PM_DASHBOARD_PATH", "dashboard.html"))
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    state = load_json(args.state_path)
    scan_cache = load_json(args.scan_cache_path)

    if state is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report = {
            "generated_at_utc": now,
            "health": "BOOTSTRAPPING",
            "alerts": [f"state file not found or invalid JSON: {args.state_path}"],
            "state": {
                "generated_at": None,
                "age_minutes": None,
                "realized_pnl": 0.0,
                "consecutive_losses": 0,
                "circuit_breaker_tripped": False,
                "open_positions": 0,
                "closed_trades": 0,
                "win_rate_pct": 0.0,
                "last_trade_exit_time": None,
                "pnl_24h": 0.0,
                "trades_24h": 0,
                "pnl_today_utc": 0.0,
                "trades_today_utc": 0,
            },
            "market_scan": {
                "seen": 0,
                "kept": 0,
                "generated_at": None,
                "cache_generated_at": scan_cache.get("generated_at") if isinstance(scan_cache, dict) else None,
                "cache_count": int(fnum(scan_cache.get("count"), 0.0)) if isinstance(scan_cache, dict) else None,
                "error": None,
            },
            "trade_filters": {
                "observed_markets": 0,
                "trading_universe": 0,
                "tradeable_markets": 0,
                "blocked_reasons": {},
            },
            "dashboard": {
                "path": args.dashboard_path,
                "exists": os.path.exists(args.dashboard_path),
                "age_minutes": None,
            },
            "learning": {
                "setup_count": 0,
                "sampled_setup_count": 0,
                "min_trades_hint": 8,
                "top": [],
                "bottom": [],
            },
            "recent_trades": [],
        }
    else:
        report = compute_report(state, scan_cache, args.dashboard_path)

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
