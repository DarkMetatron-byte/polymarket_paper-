"""Paper trader runner.

Paper trading only.

Strategy (mean reversion on 'Up' outcome in Up/Down markets):
- If in uptrend and mid YES(Up) < 0.40 -> BUY (max $10 per trade)
- If holding and mid YES(Up) > 0.60 -> SELL (close)

Circuit breaker:
- Stop opening new trades after 3 consecutive realized losses.

State:
- Tracked in paper_state.json

Run this every 15 minutes via cron.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from polymarket_client import (
    GammaClient,
    discover_active_crypto_updown_markets,
    get_yes_midprice_for_outcome,
)


STATE_PATH = os.environ.get("PM_STATE_PATH", "paper_state.json")
DASHBOARD_PATH = os.environ.get("PM_DASHBOARD_PATH", "dashboard.html")

MAX_USD_PER_TRADE = 10.0
MAX_CONSEC_LOSSES = 3

# Liquidity filter
MAX_SPREAD = 0.03

# Mispricing engine config (v1)
from mispricing_engine import (
    MispricingConfig,
    compute_edge,
    entry_signal,
    entry_threshold,
    estimate_probability_v1,
    exit_signal,
    get_history_prices,
    spread_ok,
)

# Phase-1 centralized trader config (single source of truth for trading thresholds)
TRADER_CFG: Dict[str, Any] = {
    "EDGE_ENTER": 0.10,
    "EDGE_EXIT": 0.01,
    "EDGE_ADVERSE_EXIT": -0.02,
    "MAX_SPREAD": 0.03,
    "MIN_BEST_SIZE": 50.0,
    "COOLDOWN_MINUTES": 60,
    "MAX_HOLD_MINUTES": 6 * 60,
    "MIN_PRICE": 0.05,
    "MAX_PRICE": 0.95,

    # Phase-2: market quality scoring
    "QUALITY_WEIGHTS": {
        "spread": 0.25,
        "liquidity": 0.25,
        "price_zone": 0.15,
        "time_to_resolution": 0.15,
        "activity": 0.20,
    },
    "MIN_MARKET_QUALITY_SCORE": 60.0,
}

CFG = MispricingConfig(
    sma_window=8,
    momentum_k=0.5,
    edge_enter=float(TRADER_CFG["EDGE_ENTER"]),
    edge_exit=float(TRADER_CFG["EDGE_EXIT"]),
    max_spread=float(TRADER_CFG["MAX_SPREAD"]),
)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_utc(ts: str) -> float | None:
    """Parse our stored utc_now() timestamps to epoch seconds."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        # format: 2026-03-08T22:38:00Z
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except Exception:
        return None


def minutes_since(ts: str) -> float | None:
    t = _parse_utc(ts)
    if t is None:
        return None
    return (time.time() - t) / 60.0


def best_size_snapshot(market: Dict[str, Any]) -> Dict[str, float | None]:
    """Best bid/ask size snapshot for the YES book (when available)."""
    out: Dict[str, float | None] = {"bestBidSize": None, "bestAskSize": None}
    for k in ("bestBidSize", "bestAskSize"):
        v = market.get(k)
        try:
            out[k] = float(v) if v is not None else None
        except Exception:
            out[k] = None
    return out


def liquidity_ok(market: Dict[str, Any], *, min_best_size: float) -> bool:
    ss = best_size_snapshot(market)
    bbs = ss.get("bestBidSize")
    bas = ss.get("bestAskSize")
    # If sizes are missing, don't hard-fail (backwards compatibility with older API payloads)
    if bbs is None or bas is None:
        return True
    return (bbs >= min_best_size) and (bas >= min_best_size)


def in_price_band(*, side: str, ask_yes: float, ask_no: float, min_price: float, max_price: float) -> bool:
    """Price band check applied to the entry price of the instrument we buy.

    - YES entry uses ask_yes
    - NO entry uses ask_no (i.e. NO price)
    """
    s = (side or "YES").upper()
    px = ask_no if s == "NO" else ask_yes
    return (px >= min_price) and (px <= max_price)


def is_in_cooldown(state: Dict[str, Any], market_id: str, *, cooldown_minutes: int) -> bool:
    cds = state.get("cooldowns")
    if not isinstance(cds, dict):
        return False
    until = cds.get(str(market_id))
    if not until:
        return False
    mins = minutes_since(str(until))
    # We store cooldown_until; if now < until -> still cooling down
    t_until = _parse_utc(str(until))
    if t_until is None:
        return False
    return time.time() < t_until


def set_cooldown(state: Dict[str, Any], market_id: str, *, cooldown_minutes: int) -> None:
    cds = state.setdefault("cooldowns", {})
    if not isinstance(cds, dict):
        cds = {}
        state["cooldowns"] = cds
    t_until = time.time() + float(cooldown_minutes) * 60.0
    cds[str(market_id)] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_until))


def clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def compute_market_quality_score(
    state: Dict[str, Any],
    market: Dict[str, Any],
    *,
    yes_mid: float,
    ask_yes: float | None = None,
    spread: float | None = None,
    liquidity: Dict[str, float | None] | None = None,
) -> float:
    """Compute market quality score in [0,100]. Best-effort & robust.

    Components (each mapped to 0..1):
    - spread
    - liquidity (top-of-book sizes)
    - price_zone (avoid extreme prices)
    - time_to_resolution
    - activity (freshness of our recent price updates)
    """

    w = TRADER_CFG.get("QUALITY_WEIGHTS")
    if not isinstance(w, dict):
        w = {}

    # 1) Spread score: 0 at MAX_SPREAD, 1 at 0
    max_spread = float(TRADER_CFG["MAX_SPREAD"])
    s = spread
    if s is None:
        # fall back to bb/ba if not passed
        try:
            bb = float(market.get("bestBid"))
            ba = float(market.get("bestAsk"))
            if bb <= ba:
                s = ba - bb
        except Exception:
            s = None
    if s is None:
        spread_score = 0.5
    else:
        spread_score = clamp01(1.0 - (float(s) / max_spread))

    # 2) Liquidity score: based on min(bestBidSize, bestAskSize) vs MIN_BEST_SIZE
    min_best = float(TRADER_CFG["MIN_BEST_SIZE"])
    liq = liquidity or best_size_snapshot(market)
    bbs = liq.get("bestBidSize")
    bas = liq.get("bestAskSize")
    if bbs is None or bas is None:
        liq_score = 0.5
    else:
        liq_score = clamp01(min(float(bbs), float(bas)) / min_best)

    # 3) Price zone: prefer mid prices away from extremes.
    # Map distance from 0.5 to score: 1 at 0.5, 0 at 0/1
    price_zone_score = clamp01(1.0 - (abs(float(yes_mid) - 0.5) / 0.5))

    # 4) Time to resolution: prefer not-too-far and not-immediate. Best-effort.
    # Use endDate/end_date if parseable: 1 near 7-30 days, taper outside.
    ttr_score = 0.5
    end = market.get("endDate") or market.get("end_date") or market.get("end")
    if isinstance(end, str) and end:
        # accept "2026-03-08T00:00:00Z" style
        try:
            # try full ISO first
            end_epoch = _parse_utc(end.replace(".000", ""))
            if end_epoch is None:
                # some APIs may provide without Z
                if end.endswith("Z") is False and "T" in end:
                    end_epoch = _parse_utc(end + "Z")
            if end_epoch is not None:
                mins = (end_epoch - time.time()) / 60.0
                days = mins / (60.0 * 24.0)
                # target band 7..30 days => score 1
                if days <= 0:
                    ttr_score = 0.0
                elif 7.0 <= days <= 30.0:
                    ttr_score = 1.0
                elif days < 7.0:
                    ttr_score = clamp01(days / 7.0)
                else:
                    # beyond 30 days, decay with horizon; 120d => ~0
                    ttr_score = clamp01(1.0 - ((days - 30.0) / 90.0))
        except Exception:
            ttr_score = 0.5

    # 5) Activity / freshness: based on our stored price history recency
    # If last update within 30min => 1, within 4h => decays, else 0
    act_score = 0.5
    try:
        hist = state.get("price_history", {}).get(str(market.get("id")), [])
        if isinstance(hist, list) and hist:
            last = hist[-1]
            ts = None
            if isinstance(last, dict):
                ts = last.get("t")
            if isinstance(ts, str) and ts:
                m_ago = minutes_since(ts)
                if m_ago is None:
                    act_score = 0.5
                elif m_ago <= 30:
                    act_score = 1.0
                elif m_ago <= 240:
                    act_score = clamp01(1.0 - ((m_ago - 30.0) / 210.0))
                else:
                    act_score = 0.0
    except Exception:
        act_score = 0.5

    weights = {
        "spread": float(w.get("spread", 0.25)),
        "liquidity": float(w.get("liquidity", 0.25)),
        "price_zone": float(w.get("price_zone", 0.15)),
        "time_to_resolution": float(w.get("time_to_resolution", 0.15)),
        "activity": float(w.get("activity", 0.20)),
    }
    sw = sum(weights.values()) or 1.0

    score01 = (
        spread_score * weights["spread"]
        + liq_score * weights["liquidity"]
        + price_zone_score * weights["price_zone"]
        + ttr_score * weights["time_to_resolution"]
        + act_score * weights["activity"]
    ) / sw

    return float(clamp01(score01) * 100.0)


def compute_priority_score(*, edge: float, quality: float) -> float:
    """Priority combines signal strength (edge) with market quality.

    Simple v2 formula: edge * (quality/100).
    """
    return float(edge) * (float(quality) / 100.0)


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {
            "generated_at": None,
            "consecutive_losses": 0,
            "positions": {},  # market_id -> {shares, entry_price, entry_time, slug, question}
            "trades": [],  # list of completed trades
            "realized_pnl": 0.0,
            "price_history": {},  # market_id -> [{"t": <utc>, "p": <float>}, ...]
            "spread_history": [],  # [{"t": <utc>, "spread": <float>, "market_id": <str>}, ...]
            "spread_skips": 0,
        }
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        s = json.load(f)
    # Backfill new keys for older state files
    s.setdefault("spread_history", [])
    s.setdefault("spread_skips", 0)
    s.setdefault("cooldowns", {})
    return s


def save_state(state: Dict[str, Any]) -> None:
    state["generated_at"] = utc_now()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def record_price(state: Dict[str, Any], market_id: str, price: float) -> None:
    hist = state.setdefault("price_history", {}).setdefault(market_id, [])
    if not isinstance(hist, list):
        hist = []
        state["price_history"][market_id] = hist
    hist.append({"t": utc_now(), "p": float(price)})
    # keep last 32 points (~8 hours at 15-min cadence)
    if len(hist) > 32:
        del hist[:-32]


def record_spread(state: Dict[str, Any], market_id: str, spread: float) -> None:
    sh = state.setdefault("spread_history", [])
    if not isinstance(sh, list):
        sh = []
        state["spread_history"] = sh
    sh.append({"t": utc_now(), "market_id": str(market_id), "spread": float(spread)})
    if len(sh) > 5000:
        del sh[:-5000]


def sma(values: List[float]) -> float:
    return sum(values) / len(values)


def open_position(
    state: Dict[str, Any],
    market: Dict[str, Any],
    price: float,
    *,
    side: str,
    edge: float,
    p_hat: float,
    spread: float | None = None,
    liquidity: Dict[str, float | None] | None = None,
    market_quality_score: float | None = None,
    priority_score: float | None = None,
) -> None:
    market_id = str(market.get("id"))
    usd = MAX_USD_PER_TRADE
    shares = usd / price

    liq = liquidity or {}
    state["positions"][market_id] = {
        "market_id": market_id,
        "slug": market.get("slug"),
        "question": market.get("question") or market.get("title"),
        "side": side,  # YES (Up) or NO (Down)
        "entry_time": utc_now(),
        "entry_price": price,
        "entry_edge": float(edge),
        "entry_p_hat": float(p_hat),
        "entry_spread": float(spread) if spread is not None else None,
        "entry_liquidity": {
            "bestBidSize": liq.get("bestBidSize"),
            "bestAskSize": liq.get("bestAskSize"),
        },
        "market_quality_score": float(market_quality_score) if market_quality_score is not None else None,
        "priority_score": float(priority_score) if priority_score is not None else None,
        "usd": usd,
        "shares": shares,
    }


def close_position(
    state: Dict[str, Any],
    market: Dict[str, Any],
    price: float,
    *,
    edge: float,
    p_hat: float,
    exit_reason: str,
    spread: float | None = None,
    liquidity: Dict[str, float | None] | None = None,
) -> None:
    market_id = str(market.get("id"))
    pos = state["positions"].pop(market_id, None)
    if not pos:
        return

    # PnL depends on side:
    # - YES: position value = shares * p
    # - NO:  position value = shares * (1 - p)
    side = (pos.get("side") or "YES").upper()
    if side == "NO":
        proceeds = pos["shares"] * (1.0 - price)
    else:
        proceeds = pos["shares"] * price

    pnl = proceeds - pos["usd"]

    exit_time = utc_now()
    hold_mins = None
    ms = minutes_since(str(pos.get("entry_time") or ""))
    if ms is not None:
        hold_mins = float(ms)

    liq = liquidity or {}

    trade = {
        "market_id": market_id,
        "slug": pos.get("slug") or market.get("slug"),
        "question": pos.get("question") or market.get("question") or market.get("title"),
        "side": side,
        "entry_time": pos.get("entry_time"),
        "exit_time": exit_time,
        "hold_minutes": hold_mins,
        "entry_price": pos.get("entry_price"),
        "exit_price": price,
        "entry_spread": pos.get("entry_spread"),
        "exit_spread": float(spread) if spread is not None else None,
        "entry_liquidity": pos.get("entry_liquidity"),
        "exit_liquidity": {
            "bestBidSize": liq.get("bestBidSize"),
            "bestAskSize": liq.get("bestAskSize"),
        },
        "market_quality_score": pos.get("market_quality_score"),
        "priority_score": pos.get("priority_score"),
        "entry_edge": pos.get("entry_edge"),
        "exit_edge": float(edge),
        "entry_p_hat": pos.get("entry_p_hat"),
        "exit_p_hat": float(p_hat),
        "usd": pos.get("usd"),
        "shares": pos.get("shares"),
        "pnl": pnl,
        "pnl_pct": (pnl / pos["usd"]) if pos.get("usd") else None,
        "exit_reason": str(exit_reason),
    }
    state["trades"].append(trade)
    state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + pnl

    # Start cooldown timer for this market after an exit
    set_cooldown(state, market_id, cooldown_minutes=int(TRADER_CFG["COOLDOWN_MINUTES"]))

    if pnl < 0:
        state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
    else:
        state["consecutive_losses"] = 0


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _render_scan_section(scan: Dict[str, Any]) -> str:
    if not scan:
        return "<div class='muted'>No scan data.</div>"

    top = scan.get("top") or []
    if not isinstance(top, list):
        top = []

    # Build rows
    rows = "\n".join(
        "<tr>"
        f"<td>{html_escape(str(r.get('end','')))}</td>"
        f"<td>{html_escape(str(r.get('slug','')))}</td>"
        f"<td>{html_escape(str(r.get('question','')))}</td>"
        f"<td style='text-align:right'>{float(r.get('spread') or 0.0):.3f}</td>"
        f"<td style='text-align:right'>{float(r.get('bestBid') or 0.0):.3f}</td>"
        f"<td style='text-align:right'>{float(r.get('bestAsk') or 0.0):.3f}</td>"
        "</tr>"
        for r in top[:25]
        if isinstance(r, dict)
    )

    flags = scan.get('flag_counts') or {}
    if isinstance(flags, dict) and flags:
        flag_txt = ", ".join(f"{k}:{int(v)}" for k, v in sorted(flags.items(), key=lambda kv: kv[0]))
    else:
        flag_txt = "none"

    return f"""
<div class='muted'>Generated: {html_escape(str(scan.get('generated_at','')))} | Seen: {int(scan.get('total_seen',0))} | Kept: {int(scan.get('total_kept',0))} | Flags: {html_escape(flag_txt)}</div>
<table>
  <thead>
    <tr><th>End</th><th>Slug</th><th>Question</th><th style='text-align:right'>Spread</th><th style='text-align:right'>Bid</th><th style='text-align:right'>Ask</th></tr>
  </thead>
  <tbody>
    {rows or "<tr><td colspan='6' class='muted'>No markets matched the filters.</td></tr>"}
  </tbody>
</table>
"""


def write_dashboard(state: Dict[str, Any]) -> None:
    trades: List[Dict[str, Any]] = list(state.get("trades", []))
    realized_pnl = float(state.get("realized_pnl", 0.0))

    wins = sum(1 for t in trades if float(t.get("pnl", 0.0)) > 0)
    total = len(trades)
    win_rate = (wins / total) if total else 0.0

    entry_edges = [float(t.get("entry_edge") or 0.0) for t in trades]
    exit_edges = [float(t.get("exit_edge") or 0.0) for t in trades]
    avg_entry_edge = (sum(entry_edges) / len(entry_edges)) if entry_edges else 0.0
    avg_exit_edge = (sum(exit_edges) / len(exit_edges)) if exit_edges else 0.0

    # Edge buckets (by |entry_edge|)
    def _bucket(a: float) -> str:
        if a >= 0.15:
            return ">=0.15"
        if a >= 0.10:
            return "0.10-0.15"
        if a >= 0.07:
            return "0.07-0.10"
        if a >= 0.05:
            return "0.05-0.07"
        return "<0.05"

    bucket_stats = {}
    for t in trades:
        b = _bucket(abs(float(t.get("entry_edge") or 0.0)))
        s = bucket_stats.setdefault(b, {"n": 0, "wins": 0, "sum_pnl": 0.0})
        s["n"] += 1
        if float(t.get("pnl", 0.0)) > 0:
            s["wins"] += 1
        s["sum_pnl"] += float(t.get("pnl", 0.0))

    bucket_order = ["0.05-0.07", "0.07-0.10", "0.10-0.15", ">=0.15", "<0.05"]
    bucket_rows = "\n".join(
        f"<tr><td>{b}</td>"
        f"<td style='text-align:right'>{bucket_stats[b]['n']}</td>"
        f"<td style='text-align:right'>{(bucket_stats[b]['wins']/bucket_stats[b]['n']*100):.1f}%</td>"
        f"<td style='text-align:right'>{(bucket_stats[b]['sum_pnl']/bucket_stats[b]['n']):.4f}</td></tr>"
        for b in bucket_order
        if b in bucket_stats and bucket_stats[b]["n"]
    )

    spreads = [float(x.get("spread")) for x in state.get("spread_history", []) if isinstance(x, dict) and "spread" in x]
    avg_spread = (sum(spreads) / len(spreads)) if spreads else 0.0
    spread_skips = int(state.get("spread_skips", 0))

    recent = list(reversed(trades[-25:]))

    # Open positions
    positions = list(state.get("positions", {}).values())
    open_rows = "\n".join(
        f"<tr>"
        f"<td>{html_escape(str(p.get('entry_time','')))}</td>"
        f"<td>{html_escape(str(p.get('slug','')))}</td>"
        f"<td>{html_escape(str(p.get('side','')))}</td>"
        f"<td style='text-align:right'>{float(p.get('entry_price',0.0)):.3f}</td>"
        f"<td style='text-align:right'>{float(p.get('entry_edge',0.0)):.3f}</td>"
        f"<td style='text-align:right'>{float(p.get('entry_p_hat',0.0)):.3f}</td>"
        f"<td style='text-align:right'>{float(p.get('market_quality_score',0.0) or 0.0):.1f}</td>"
        f"</tr>"
        for p in positions
    )

    rows = "\n".join(
        f"<tr>"
        f"<td>{html_escape(str(t.get('exit_time','')))}</td>"
        f"<td>{html_escape(str(t.get('slug','')))}</td>"
        f"<td>{html_escape(str(t.get('side','')))}</td>"
        f"<td style='text-align:right'>{float(t.get('entry_price',0.0) or 0.0):.3f}</td>"
        f"<td style='text-align:right'>{float(t.get('exit_price',0.0) or 0.0):.3f}</td>"
        f"<td style='text-align:right'>{float(t.get('hold_minutes',0.0) or 0.0):.1f}</td>"
        f"<td style='text-align:right'>{float(t.get('entry_spread',0.0) or 0.0):.3f}</td>"
        f"<td style='text-align:right'>{float(t.get('exit_spread',0.0) or 0.0):.3f}</td>"
        f"<td style='text-align:right'>{float(t.get('entry_edge',0.0) or 0.0):.3f}</td>"
        f"<td style='text-align:right'>{float(t.get('exit_edge',0.0) or 0.0):.3f}</td>"
        f"<td>{html_escape(str(t.get('exit_reason','')))}</td>"
        f"<td style='text-align:right'>{float(t.get('pnl',0.0) or 0.0):.4f}</td>"
        f"</tr>"
        for t in recent
    )

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Polymarket Paper Trader</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }}
    .kpi {{ display: flex; gap: 24px; margin-bottom: 16px; }}
    .card {{ padding: 12px 14px; border: 1px solid #ddd; border-radius: 10px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; font-size: 14px; }}
    th {{ text-align: left; background: #fafafa; }}
    .muted {{ color: #666; font-size: 13px; }}
  </style>
</head>
<body>
  <h2>Polymarket Paper Trader</h2>
  <div class="muted">Updated: {html_escape(str(state.get('generated_at') or ''))} | Circuit breaker losses: {int(state.get('consecutive_losses',0))}/{MAX_CONSEC_LOSSES} | Open positions: {len(state.get('positions',{}))}</div>

  <div class="kpi">
    <div class="card"><div class="muted">Realized P/L</div><div><b>{realized_pnl:.4f}</b></div></div>
    <div class="card"><div class="muted">Win rate</div><div><b>{(win_rate*100):.1f}%</b> ({wins}/{total})</div></div>
    <div class="card"><div class="muted">Total trades</div><div><b>{total}</b></div></div>
    <div class="card"><div class="muted">Open positions</div><div><b>{len(state.get('positions',{}))}</b></div></div>
    <div class="card"><div class="muted">Avg entry edge</div><div><b>{avg_entry_edge:.3f}</b></div></div>
    <div class="card"><div class="muted">Avg exit edge</div><div><b>{avg_exit_edge:.3f}</b></div></div>
    <div class="card"><div class="muted">Avg spread</div><div><b>{avg_spread:.3f}</b></div></div>
    <div class="card"><div class="muted">Spread skips</div><div><b>{spread_skips}</b></div></div>
  </div>

  <h3>Market scan (filters: spread ≤ 0.03, end ≤ 30 days, not resolved)</h3>
  {_render_scan_section(state.get('market_scan') or {})}

  <h3 style="margin-top:18px">Open positions</h3>
  <table>
    <thead>
      <tr><th>Entry time (UTC)</th><th>Market</th><th>Side</th><th>Entry</th><th>Entry edge</th><th>Entry p̂</th><th style='text-align:right'>Quality</th></tr>
    </thead>
    <tbody>
      {open_rows or '<tr><td colspan="7" class="muted">No open positions</td></tr>'}
    </tbody>
  </table>

  <h3 style="margin-top:18px">Edge buckets (|entry_edge|)</h3>
  <table>
    <thead>
      <tr><th>Bucket</th><th style='text-align:right'>N</th><th style='text-align:right'>Win%</th><th style='text-align:right'>Avg P/L</th></tr>
    </thead>
    <tbody>
      {bucket_rows or '<tr><td colspan="4" class="muted">No trades yet</td></tr>'}
    </tbody>
  </table>

  <h3 style="margin-top:18px">Recent trades</h3>
  <table>
    <thead>
      <tr>
        <th>Exit time (UTC)</th>
        <th>Market</th>
        <th>Side</th>
        <th style='text-align:right'>Entry</th>
        <th style='text-align:right'>Exit</th>
        <th style='text-align:right'>Hold (min)</th>
        <th style='text-align:right'>Entry spread</th>
        <th style='text-align:right'>Exit spread</th>
        <th style='text-align:right'>Entry edge</th>
        <th style='text-align:right'>Exit edge</th>
        <th>Exit reason</th>
        <th style='text-align:right'>P/L</th>
      </tr>
    </thead>
    <tbody>
      {rows or '<tr><td colspan="12" class="muted">No trades yet</td></tr>'}
    </tbody>
  </table>
</body>
</html>
"""

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def main() -> int:
    start_ts = utc_now()
    print(f"[{start_ts}] trader: start", flush=True)
    state = load_state()

    # Market scan (all markets) for dashboard
    try:
        from market_scan import ScanConfig, scan_markets

        state["market_scan"] = scan_markets(GammaClient(), ScanConfig(max_spread=0.03, days_ahead=30, pages=20, page_size=100))
    except Exception as e:
        state["market_scan_error"] = str(e)

    # Prefer cached markets (written by discover_markets.py). Fallback to live discovery.
    markets = None
    try:
        with open("markets_cache.json", "r", encoding="utf-8") as f:
            cache = json.load(f)
        markets = cache.get("markets")
        if not isinstance(markets, list):
            markets = None
    except Exception:
        markets = None

    if markets is None:
        c = GammaClient()
        markets = discover_active_crypto_updown_markets(c, pages=5, page_size=100)

    # Circuit breaker: stop opening new trades, but still allow closing.
    cb_tripped = int(state.get("consecutive_losses", 0)) >= MAX_CONSEC_LOSSES

    # Collect entry candidates; choose best by priority_score.
    candidates: List[Dict[str, Any]] = []

    for m in markets:
        market_id = str(m.get("id"))
        price = get_yes_midprice_for_outcome(m, "Up")
        if price is None:
            continue

        # Update our internal price history first
        record_price(state, market_id, price)

        has_pos = market_id in state.get("positions", {})

        # Compute mispricing (needs enough history)
        p_hat = estimate_probability_v1(state, market_id, price, CFG)
        if p_hat is None:
            continue

        # Exit rules (exactly one exit_reason)
        if has_pos:
            pos = state.get("positions", {}).get(market_id, {})
            pos_side = (pos.get("side") or "YES").upper()

            # Compute a more realistic exit edge using crossing price
            bb = m.get("bestBid")
            ba = m.get("bestAsk")
            try:
                bb_f = float(bb) if bb is not None else None
                ba_f = float(ba) if ba is not None else None
            except Exception:
                bb_f, ba_f = None, None

            bid_yes = bb_f if bb_f is not None else price
            bid_no = (1.0 - ba_f) if ba_f is not None else (1.0 - price)
            exit_edge_yes = float(p_hat) - float(bid_yes)
            exit_edge_no = (1.0 - float(p_hat)) - float(bid_no)
            exit_edge = exit_edge_no if pos_side == "NO" else exit_edge_yes

            # 1) Time-stop
            hold_mins = minutes_since(str(pos.get("entry_time") or ""))
            # capture exit spread/liquidity snapshot (for logging)
            exit_spread = None
            if bb_f is not None and ba_f is not None:
                exit_spread = ba_f - bb_f
            exit_liq = best_size_snapshot(m)

            if hold_mins is not None and hold_mins >= float(TRADER_CFG["MAX_HOLD_MINUTES"]):
                close_position(state, m, price, edge=exit_edge, p_hat=p_hat, exit_reason="TIME_STOP", spread=exit_spread, liquidity=exit_liq)
                continue

            # 2) Adverse edge
            if exit_edge <= float(TRADER_CFG["EDGE_ADVERSE_EXIT"]):
                close_position(state, m, price, edge=exit_edge, p_hat=p_hat, exit_reason="EDGE_ADVERSE_EXIT", spread=exit_spread, liquidity=exit_liq)
                continue

            # 3) Normal edge exit (existing behavior)
            if exit_edge <= float(TRADER_CFG["EDGE_EXIT"]):
                close_position(state, m, price, edge=exit_edge, p_hat=p_hat, exit_reason="EDGE_EXIT", spread=exit_spread, liquidity=exit_liq)
                continue

        # Entry rule: only if allowed + passes hygiene checks
        if (not has_pos) and (not cb_tripped) and spread_ok(m, CFG):
            # Cooldown after exit
            if is_in_cooldown(state, market_id, cooldown_minutes=int(TRADER_CFG["COOLDOWN_MINUTES"])):
                continue

            points = len(get_history_prices(state, market_id))
            th = entry_threshold(CFG, points=points)

            bb = m.get("bestBid")
            ba = m.get("bestAsk")
            try:
                bb_f = float(bb) if bb is not None else None
                ba_f = float(ba) if ba is not None else None
            except Exception:
                bb_f, ba_f = None, None

            # Spread filter
            spread = None
            if bb_f is not None and ba_f is not None:
                spread = ba_f - bb_f
                record_spread(state, market_id, spread)
                if spread > float(TRADER_CFG["MAX_SPREAD"]):
                    state["spread_skips"] = int(state.get("spread_skips", 0)) + 1
                    print(f"Skipped market due to large spread: {market_id} spread={spread:.4f}")
                    continue

            # Liquidity filter (best sizes)
            if not liquidity_ok(m, min_best_size=float(TRADER_CFG["MIN_BEST_SIZE"])):
                print(f"Skipped market due to low best-size liquidity: {market_id}")
                continue

            # Use *realistic entry price* (crossing the spread):
            # - YES buys pay ask
            # - NO buys pay ask_no = 1 - bestBid_yes (approx, using same book)
            ask_yes = ba_f if ba_f is not None else price
            ask_no = (1.0 - bb_f) if bb_f is not None else (1.0 - price)

            edge_yes = float(p_hat) - float(ask_yes)
            edge_no = (1.0 - float(p_hat)) - float(ask_no)

            # Price band filter (apply to the instrument we're buying)
            if (edge_yes >= th) and (not in_price_band(side="YES", ask_yes=ask_yes, ask_no=ask_no, min_price=float(TRADER_CFG["MIN_PRICE"]), max_price=float(TRADER_CFG["MAX_PRICE"]))):
                continue
            if (edge_no >= th) and (not in_price_band(side="NO", ask_yes=ask_yes, ask_no=ask_no, min_price=float(TRADER_CFG["MIN_PRICE"]), max_price=float(TRADER_CFG["MAX_PRICE"]))):
                continue

            liq_snap = best_size_snapshot(m)

            # Phase-2: market quality gate + prioritization
            quality = compute_market_quality_score(state, m, yes_mid=price, ask_yes=ask_yes, spread=spread, liquidity=liq_snap)
            if quality < float(TRADER_CFG["MIN_MARKET_QUALITY_SCORE"]):
                continue

            # Decide entry based on the edge at the *actual* entry price
            enter_th = max(float(TRADER_CFG["EDGE_ENTER"]), th)

            if edge_yes >= enter_th:
                pr = compute_priority_score(edge=edge_yes, quality=quality)
                candidates.append(
                    {
                        "market": m,
                        "market_id": market_id,
                        "side": "YES",
                        "entry_price": ask_yes,
                        "edge": edge_yes,
                        "p_hat": p_hat,
                        "spread": spread,
                        "liq": liq_snap,
                        "quality": quality,
                        "priority": pr,
                    }
                )
            if edge_no >= enter_th:
                pr = compute_priority_score(edge=edge_no, quality=quality)
                candidates.append(
                    {
                        "market": m,
                        "market_id": market_id,
                        "side": "NO",
                        "entry_price": ask_no,
                        "edge": edge_no,
                        "p_hat": p_hat,
                        "spread": spread,
                        "liq": liq_snap,
                        "quality": quality,
                        "priority": pr,
                    }
                )

    # Execute at most one new entry per run: best priority_score
    if candidates:
        best = max(candidates, key=lambda c: float(c.get("priority", 0.0)))
        open_position(
            state,
            best["market"],
            float(best["entry_price"]),
            side=str(best["side"]),
            edge=float(best["edge"]),
            p_hat=float(best["p_hat"]),
            spread=best.get("spread"),
            liquidity=best.get("liq"),
            market_quality_score=float(best.get("quality", 0.0)),
            priority_score=float(best.get("priority", 0.0)),
        )

    save_state(state)
    write_dashboard(state)

    end_ts = utc_now()
    ms = state.get("market_scan") if isinstance(state.get("market_scan"), dict) else {}
    total_seen = ms.get("total_seen")
    total_kept = ms.get("total_kept")
    n_positions = len(state.get("positions") or {}) if isinstance(state.get("positions"), dict) else 0
    n_trades = len(state.get("trades") or []) if isinstance(state.get("trades"), list) else 0
    print(
        f"[{end_ts}] trader: done market_scan(seen={total_seen}, kept={total_kept}) positions={n_positions} trades={n_trades}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    # Optional CLI: allow a clean one-shot run when invoked by a daemon/scheduler.
    import argparse

    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--once", action="store_true", help="Run a single cycle then exit (default behavior).")
    _ = ap.parse_args()

    raise SystemExit(main())
