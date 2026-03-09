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

import calendar
import json
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

# File locking to prevent overlapping cron/systemd runs (Unix only).
try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:  # Windows / fallback
    _HAS_FCNTL = False

from config import CACHE_PATH, DASHBOARD_PATH, STATE_PATH
from polymarket_client import (
    GammaClient,
    discover_active_crypto_updown_markets,
    get_yes_midprice_for_outcome,
)

MAX_USD_PER_TRADE  = 10.0
MAX_CONSEC_LOSSES  = 3
MAX_TRADES_STATE   = 500   # max completed trades kept in paper_state.json

# Liquidity filter
MAX_SPREAD = 0.03

# Market intelligence layer (structural pre-filter, runs before edge evaluation)
from market_intelligence import (
    MarketIntelConfig,
    TRADEABLE_CLASSES,
    compute_market_intel,
)

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

    # Portfolio limits (Points 2 + 5)
    "MAX_OPEN_POSITIONS":      3,   # max concurrent open positions
    "MAX_POSITIONS_PER_ASSET": 1,   # max positions in same underlying (BTC/ETH/SOL)
    "MAX_ENTRIES_PER_CYCLE":   2,   # max new entries opened per 15-min cycle

    # Circuit breaker thresholds (expressed as multiples of MAX_USD_PER_TRADE
    # so they scale automatically when you change position size)
    "CB_MAX_DRAWDOWN_MULT":   5.0,   # trip if total P/L < -(5 × trade_size)
    "CB_ROLLING_WINDOW":       5,    # look-back window for rolling loss check
    "CB_ROLLING_LOSS_MULT":   2.0,   # trip if last-N trades sum < -(2 × trade_size)
}

CFG = MispricingConfig(
    sma_window=8,
    momentum_k=0.5,
    edge_enter=float(TRADER_CFG["EDGE_ENTER"]),
    edge_exit=float(TRADER_CFG["EDGE_EXIT"]),
    max_spread=float(TRADER_CFG["MAX_SPREAD"]),
)

# Market Intelligence config — structural pre-filter thresholds.
MI_CFG = MarketIntelConfig()

# Binance external signal config.
from binance_client import BinanceClient
from signal_engine import BinanceSnapshot, SignalConfig, blend_p_hat, compute_binance_signal

SIGNAL_CFG = SignalConfig()


_ASSET_KW: Dict[str, List[str]] = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
}


def _detect_asset(market: Dict[str, Any]) -> Optional[str]:
    """Return the underlying asset key (BTC/ETH/SOL) for a market, or None."""
    text = " ".join(str(market.get(k) or "") for k in ("question", "title", "slug")).lower()
    for asset, keywords in _ASSET_KW.items():
        if any(kw in text for kw in keywords):
            return asset
    return None


def _count_open_asset_positions(state: Dict[str, Any], asset: Optional[str]) -> int:
    """Count open positions whose underlying matches *asset*.

    Uses the stored 'asset' key when available (set by open_position), with
    a fallback to text-based detection for positions opened before this field existed.
    """
    if asset is None:
        return 0
    count = 0
    for pos in state.get("positions", {}).values():
        stored = pos.get("asset")
        if stored is not None:
            if stored == asset:
                count += 1
        else:
            if _detect_asset({"question": pos.get("question", ""), "slug": pos.get("slug", "")}) == asset:
                count += 1
    return count


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_utc(ts: str) -> float | None:
    """Parse our stored utc_now() timestamps to epoch seconds.

    Uses calendar.timegm so the result is always UTC-correct regardless of the
    local timezone of the machine (important on VPS deployments).
    """
    if not ts or not isinstance(ts, str):
        return None
    try:
        # format: 2026-03-08T22:38:00Z
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
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
            bb_raw = market.get("bestBid")
            ba_raw = market.get("bestAsk")
            if bb_raw is not None and ba_raw is not None:
                bb = float(bb_raw)
                ba = float(ba_raw)
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


def _prune_state(state: Dict[str, Any]) -> None:
    """Remove stale / unbounded data before saving.

    Keeps paper_state.json small across long deployments:
    - trades[]         → last MAX_TRADES_STATE entries (realized_pnl is a running total, unaffected)
    - cooldowns{}      → remove entries whose timestamp has already expired
    - price_history{}  → remove markets not updated in > 7 days (resolved / delisted)
    """
    # 1) Trade log
    trades = state.get("trades")
    if isinstance(trades, list) and len(trades) > MAX_TRADES_STATE:
        state["trades"] = trades[-MAX_TRADES_STATE:]

    # 2) Expired cooldowns
    cds = state.get("cooldowns")
    if isinstance(cds, dict):
        now = time.time()
        expired = [mid for mid, until in cds.items()
                   if (_parse_utc(str(until)) or 0.0) < now]
        for mid in expired:
            del cds[mid]

    # 3) Stale price history (market not seen in > 7 days)
    ph = state.get("price_history")
    if isinstance(ph, dict):
        cutoff = time.time() - 7 * 24 * 3600
        stale = []
        for mid, hist in ph.items():
            if not isinstance(hist, list) or not hist:
                stale.append(mid)
                continue
            last = hist[-1]
            t = _parse_utc(str(last.get("t") or "")) if isinstance(last, dict) else None
            if t is not None and t < cutoff:
                stale.append(mid)
        for mid in stale:
            del ph[mid]


def save_state(state: Dict[str, Any]) -> None:
    """Write state atomically (temp file + os.replace) to avoid corruption on crash."""
    state["generated_at"] = utc_now()
    state_dir = os.path.dirname(os.path.abspath(STATE_PATH))
    os.makedirs(state_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=state_dir, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tf:
        json.dump(state, tf, ensure_ascii=False, indent=2)
        tmp_path = tf.name
    os.replace(tmp_path, STATE_PATH)


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
    mi_classification: str | None = None,
    asset: str | None = None,
) -> None:
    market_id = str(market.get("id"))
    usd = MAX_USD_PER_TRADE
    shares = usd / price

    liq = liquidity or {}
    state["positions"][market_id] = {
        "market_id": market_id,
        "slug": market.get("slug"),
        "question": market.get("question") or market.get("title"),
        "asset": asset,
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
        "mi_classification": mi_classification,
        "usd": usd,
        "shares": shares,
    }


def close_position(
    state: Dict[str, Any],
    market: Dict[str, Any],
    price: float,
    *,
    exit_instrument_price: float | None = None,
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

    # PnL: use the actual fill price for the held instrument when available.
    # - YES: sell at bid_yes
    # - NO:  sell at bid_no = 1 - ask_yes
    # Fallback to mid-based estimate when orderbook prices are absent.
    side = (pos.get("side") or "YES").upper()
    if exit_instrument_price is not None:
        proceeds = pos["shares"] * exit_instrument_price
        recorded_exit_price = exit_instrument_price
    elif side == "NO":
        proceeds = pos["shares"] * (1.0 - price)
        recorded_exit_price = 1.0 - price
    else:
        proceeds = pos["shares"] * price
        recorded_exit_price = price

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
        "exit_price": recorded_exit_price,
        "entry_spread": pos.get("entry_spread"),
        "exit_spread": float(spread) if spread is not None else None,
        "entry_liquidity": pos.get("entry_liquidity"),
        "exit_liquidity": {
            "bestBidSize": liq.get("bestBidSize"),
            "bestAskSize": liq.get("bestAskSize"),
        },
        "market_quality_score": pos.get("market_quality_score"),
        "priority_score": pos.get("priority_score"),
        "mi_classification": pos.get("mi_classification"),
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


_MI_CSS_CLASS = {
    "HIGH_QUALITY": "mi-high",
    "NORMAL":       "mi-normal",
    "LOW_QUALITY":  "mi-low",
    "AVOID":        "mi-avoid",
}


def _mi_badge(cls: str) -> str:
    """Render a coloured HTML badge for a market intelligence classification."""
    css = _MI_CSS_CLASS.get(str(cls), "mi-normal")
    return f"<span class='{css}'>{html_escape(str(cls))}</span>"


def _render_binance_section(binance: Dict[str, Any]) -> str:
    if not binance:
        return "<div class='muted'>No Binance data yet.</div>"

    ts = html_escape(str(binance.get("fetched_at", "")))
    errors = binance.get("errors") or {}
    assets = binance.get("assets") or {}
    fetch_error = binance.get("fetch_error", False)

    rows = ""
    for asset in ("BTC", "ETH", "SOL"):
        if fetch_error:
            rows += (
                f"<tr>"
                f"<td>{asset}</td>"
                f"<td style='text-align:right' class='muted'>—</td>"
                f"<td style='text-align:right' class='muted'>—</td>"
                f"<td style='text-align:right' class='muted'>—</td>"
                f"</tr>"
            )
            continue
        if asset not in assets:
            err = errors.get(asset, "no data")
            rows += f"<tr><td>{asset}</td><td colspan='3' class='muted'>{html_escape(err)}</td></tr>"
            continue
        d = assets[asset]
        ret = float(d.get("return_pct", 0.0))
        vol = float(d.get("vol_pct", 0.0))
        ret_color = "#155724" if ret >= 0 else "#721c24"
        sign = "+" if ret >= 0 else ""
        rows += (
            f"<tr>"
            f"<td>{asset} <span class='muted'>({html_escape(str(d.get('ticker','')))})</span></td>"
            f"<td style='text-align:right'>${float(d.get('price',0)):,.2f}</td>"
            f"<td style='text-align:right;color:{ret_color}'><b>{sign}{ret:.2f}%</b></td>"
            f"<td style='text-align:right'>{vol:.3f}%</td>"
            f"</tr>"
        )

    if fetch_error:
        subtitle = (
            f"<div class='muted' style='color:#856404'>"
            f"⚠ Binance unavailable at {ts} — trading on internal model only"
            f"</div>"
        )
    else:
        subtitle = (
            f"<div class='muted'>Fetched: {ts} | "
            f"blend weight: {SIGNAL_CFG.binance_blend_weight:.0%} Binance / "
            f"{1-SIGNAL_CFG.binance_blend_weight:.0%} internal</div>"
        )

    return f"""
{subtitle}
<table>
  <thead><tr><th>Asset</th><th style='text-align:right'>Spot price</th><th style='text-align:right'>2h return</th><th style='text-align:right'>2h vol (σ)</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
"""


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
        f"<td>{_mi_badge(p.get('mi_classification') or '')}</td>"
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
        f"<td>{_mi_badge(t.get('mi_classification') or '')}</td>"
        f"<td style='text-align:right'>{float(t.get('pnl',0.0) or 0.0):.4f}</td>"
        f"</tr>"
        for t in recent
    )

    # MI by-classification breakdown (based on trade history)
    mi_cls_stats: Dict[str, Any] = {}
    for t in trades:
        cls = str(t.get("mi_classification") or "UNKNOWN")
        s = mi_cls_stats.setdefault(cls, {"n": 0, "wins": 0, "sum_pnl": 0.0})
        s["n"] += 1
        if float(t.get("pnl", 0.0)) > 0:
            s["wins"] += 1
        s["sum_pnl"] += float(t.get("pnl", 0.0))

    mi_cls_order = ["HIGH_QUALITY", "NORMAL", "LOW_QUALITY", "AVOID", "UNKNOWN"]
    mi_cls_rows = "\n".join(
        f"<tr>"
        f"<td>{_mi_badge(cls)}</td>"
        f"<td style='text-align:right'>{mi_cls_stats[cls]['n']}</td>"
        f"<td style='text-align:right'>{(mi_cls_stats[cls]['wins']/mi_cls_stats[cls]['n']*100):.1f}%</td>"
        f"<td style='text-align:right'>{(mi_cls_stats[cls]['sum_pnl']/mi_cls_stats[cls]['n']):.4f}</td>"
        f"</tr>"
        for cls in mi_cls_order
        if cls in mi_cls_stats and mi_cls_stats[cls]["n"]
    )

    # MI last-run scan stats (classification counts from most recent cycle)
    mi_run = state.get("mi_stats_last_run") or {}
    mi_run_txt = " | ".join(
        f"{_mi_badge(k)} {v}"
        for k, v in sorted(mi_run.items())
        if isinstance(v, int)
    ) or "<span class='muted'>no data</span>"

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Polymarket Paper Trader</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }}
    .kpi {{ display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 16px; }}
    .card {{ padding: 12px 14px; border: 1px solid #ddd; border-radius: 10px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; font-size: 14px; }}
    th {{ text-align: left; background: #fafafa; }}
    .muted {{ color: #666; font-size: 13px; }}
    /* Market Intelligence badges */
    .mi-high   {{ background:#d4edda; color:#155724; padding:2px 7px; border-radius:4px; font-size:12px; font-weight:bold; white-space:nowrap; }}
    .mi-normal {{ background:#cce5ff; color:#004085; padding:2px 7px; border-radius:4px; font-size:12px; font-weight:bold; white-space:nowrap; }}
    .mi-low    {{ background:#fff3cd; color:#856404; padding:2px 7px; border-radius:4px; font-size:12px; font-weight:bold; white-space:nowrap; }}
    .mi-avoid  {{ background:#f8d7da; color:#721c24; padding:2px 7px; border-radius:4px; font-size:12px; font-weight:bold; white-space:nowrap; }}
  </style>
</head>
<body>
  <h2>Polymarket Paper Trader</h2>
  <div class="muted">Updated: {html_escape(str(state.get('generated_at') or ''))} | Consec. losses: {int(state.get('consecutive_losses',0))}/{MAX_CONSEC_LOSSES} | Drawdown: {realized_pnl:.2f} (limit: {-(float(TRADER_CFG['CB_MAX_DRAWDOWN_MULT'])*MAX_USD_PER_TRADE):.0f}) | Open: {len(state.get('positions',{}))}{' | <b style="color:#721c24">⛔ CB TRIPPED: ' + html_escape(', '.join(state.get('cb_reasons') or [])) + '</b>' if state.get('cb_tripped') else ''}</div>

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

  <h3>Binance external signal — last run</h3>
  {_render_binance_section(state.get('binance_last_run') or {})}

  <h3>Market Intelligence — last run classification counts</h3>
  <div style="margin-bottom:12px">{mi_run_txt}</div>

  <h3>Market scan (filters: spread ≤ 0.03, end ≤ 30 days, not resolved)</h3>
  {_render_scan_section(state.get('market_scan') or {})}

  <h3 style="margin-top:18px">Open positions</h3>
  <table>
    <thead>
      <tr><th>Entry time (UTC)</th><th>Market</th><th>Side</th><th>Entry</th><th>Entry edge</th><th>Entry p̂</th><th style='text-align:right'>MI Score</th><th>MI Class</th></tr>
    </thead>
    <tbody>
      {open_rows or '<tr><td colspan="8" class="muted">No open positions</td></tr>'}
    </tbody>
  </table>

  <h3 style="margin-top:18px">Market Intelligence — trades by classification</h3>
  <table>
    <thead>
      <tr><th>Classification</th><th style='text-align:right'>N</th><th style='text-align:right'>Win%</th><th style='text-align:right'>Avg P/L</th></tr>
    </thead>
    <tbody>
      {mi_cls_rows or '<tr><td colspan="4" class="muted">No trades yet</td></tr>'}
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
        <th>MI Class</th>
        <th style='text-align:right'>P/L</th>
      </tr>
    </thead>
    <tbody>
      {rows or '<tr><td colspan="13" class="muted">No trades yet</td></tr>'}
    </tbody>
  </table>
</body>
</html>
"""

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def _acquire_run_lock() -> Optional[Any]:
    """Acquire an exclusive run lock to prevent overlapping cron/systemd runs.

    Unix only (fcntl). On Windows or if fcntl is unavailable, returns None (no lock).
    Exits with code 0 immediately if another instance already holds the lock.
    """
    if not _HAS_FCNTL:
        return None
    lock_path = STATE_PATH + ".lock"
    lf = open(lock_path, "w")
    try:
        _fcntl.flock(lf, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return lf
    except BlockingIOError:
        print(f"[{utc_now()}] trader: another instance is already running — exiting.", flush=True)
        lf.close()
        raise SystemExit(0)


def main() -> int:
    start_ts = utc_now()
    print(f"[{start_ts}] trader: start", flush=True)
    _lock = _acquire_run_lock()  # exits immediately if another instance is running
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
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        markets = cache.get("markets")
        if not isinstance(markets, list):
            markets = None
    except Exception:
        markets = None

    if markets is None:
        c = GammaClient()
        markets = discover_active_crypto_updown_markets(c, pages=5, page_size=100)

    # Fetch Binance spot prices + klines once per cycle.
    # Errors are non-fatal: internal model runs unchanged when Binance is down.
    binance_snap: Optional[BinanceSnapshot] = None
    try:
        binance_snap = BinanceSnapshot.fetch(BinanceClient(), cfg=SIGNAL_CFG)
        if binance_snap.errors:
            print(f"[{utc_now()}] binance: partial errors {binance_snap.errors}", flush=True)
        if binance_snap.assets:
            print(f"[{utc_now()}] binance: fetched {list(binance_snap.assets.keys())}", flush=True)
    except Exception as exc:
        print(f"[{utc_now()}] binance: fetch failed ({exc}) — internal model only", flush=True)

    # Circuit breaker: multi-condition. Always allows closing open positions.
    _cb_consec   = int(state.get("consecutive_losses", 0))
    _cb_drawdown = float(state.get("realized_pnl", 0.0))
    _cb_window   = int(TRADER_CFG["CB_ROLLING_WINDOW"])
    _recent_pnl  = [float(t.get("pnl", 0.0)) for t in list(state.get("trades", []))[-_cb_window:]]
    _rolling_pnl = sum(_recent_pnl)

    _cb_drawdown_limit = -float(TRADER_CFG["CB_MAX_DRAWDOWN_MULT"]) * MAX_USD_PER_TRADE
    _cb_rolling_limit  = -float(TRADER_CFG["CB_ROLLING_LOSS_MULT"]) * MAX_USD_PER_TRADE

    cb_reasons: List[str] = []
    if _cb_consec >= MAX_CONSEC_LOSSES:
        cb_reasons.append(f"consec={_cb_consec}")
    if _cb_drawdown < _cb_drawdown_limit:
        cb_reasons.append(f"drawdown={_cb_drawdown:.2f}<{_cb_drawdown_limit:.2f}")
    if len(_recent_pnl) >= _cb_window and _rolling_pnl < _cb_rolling_limit:
        cb_reasons.append(f"rolling={_rolling_pnl:.2f}<{_cb_rolling_limit:.2f}")

    cb_tripped = bool(cb_reasons)
    if cb_tripped:
        print(f"[{utc_now()}] circuit-breaker TRIPPED: {', '.join(cb_reasons)}", flush=True)

    # Collect entry candidates; choose best by priority_score.
    candidates: List[Dict[str, Any]] = []
    mi_stats: Dict[str, int] = {}  # classification → count (for reporting + dashboard)

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

        # Blend with Binance external signal when available.
        if binance_snap is not None:
            p_binance = compute_binance_signal(m, binance_snap, SIGNAL_CFG)
            p_hat = blend_p_hat(p_hat, p_binance, SIGNAL_CFG)

        # ── Market Intelligence pre-filter ──────────────────────────────────
        # Classify market structure BEFORE edge evaluation.
        # Exit rules always proceed — we never block closing an open position.
        # Entry rules are gated: only HIGH_QUALITY and NORMAL markets may enter.
        mi = compute_market_intel(m, mid_price=price, mi_cfg=MI_CFG)
        mi_stats[mi.classification.value] = mi_stats.get(mi.classification.value, 0) + 1

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

            # Actual fill price for the held instrument:
            # YES closes at bid_yes; NO closes at bid_no = 1 - ask_yes.
            actual_exit_price = bid_no if pos_side == "NO" else bid_yes

            # 1) Time-stop
            hold_mins = minutes_since(str(pos.get("entry_time") or ""))
            # capture exit spread/liquidity snapshot (for logging)
            exit_spread = None
            if bb_f is not None and ba_f is not None:
                exit_spread = ba_f - bb_f
            exit_liq = best_size_snapshot(m)

            if hold_mins is not None and hold_mins >= float(TRADER_CFG["MAX_HOLD_MINUTES"]):
                close_position(state, m, price, exit_instrument_price=actual_exit_price, edge=exit_edge, p_hat=p_hat, exit_reason="TIME_STOP", spread=exit_spread, liquidity=exit_liq)
                continue

            # 2) Adverse edge
            if exit_edge <= float(TRADER_CFG["EDGE_ADVERSE_EXIT"]):
                close_position(state, m, price, exit_instrument_price=actual_exit_price, edge=exit_edge, p_hat=p_hat, exit_reason="EDGE_ADVERSE_EXIT", spread=exit_spread, liquidity=exit_liq)
                continue

            # 3) Normal edge exit (existing behavior)
            if exit_edge <= float(TRADER_CFG["EDGE_EXIT"]):
                close_position(state, m, price, exit_instrument_price=actual_exit_price, edge=exit_edge, p_hat=p_hat, exit_reason="EDGE_EXIT", spread=exit_spread, liquidity=exit_liq)
                continue

        # Entry rule: only if allowed + passes hygiene checks + MI gate
        if (not has_pos) and (not cb_tripped) and spread_ok(m, CFG) and mi.is_tradeable():
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

            # Market quality gate: MI score is the structural quality (0..100).
            # MIN_MARKET_QUALITY_SCORE provides a secondary threshold within tradeable markets.
            quality = mi.market_quality_score
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
                        "mi_classification": mi.classification.value,
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
                        "mi_classification": mi.classification.value,
                    }
                )

    # Execute top-N entries per cycle (ranked by priority_score).
    # Gated by portfolio limits: max open positions + max per-asset exposure.
    if candidates:
        sorted_cands = sorted(candidates, key=lambda c: float(c.get("priority", 0.0)), reverse=True)
        entries_this_cycle = 0
        max_entries   = int(TRADER_CFG["MAX_ENTRIES_PER_CYCLE"])
        max_positions = int(TRADER_CFG["MAX_OPEN_POSITIONS"])
        max_per_asset = int(TRADER_CFG["MAX_POSITIONS_PER_ASSET"])

        for c in sorted_cands:
            if entries_this_cycle >= max_entries:
                break
            if len(state["positions"]) >= max_positions:
                break
            asset = _detect_asset(c["market"])
            if _count_open_asset_positions(state, asset) >= max_per_asset:
                continue
            open_position(
                state,
                c["market"],
                float(c["entry_price"]),
                side=str(c["side"]),
                edge=float(c["edge"]),
                p_hat=float(c["p_hat"]),
                spread=c.get("spread"),
                liquidity=c.get("liq"),
                market_quality_score=float(c.get("quality", 0.0)),
                priority_score=float(c.get("priority", 0.0)),
                mi_classification=str(c.get("mi_classification") or ""),
                asset=asset,
            )
            entries_this_cycle += 1

    # Persist circuit breaker state for the dashboard.
    state["cb_tripped"] = cb_tripped
    state["cb_reasons"] = cb_reasons

    # Persist MI run stats for the dashboard
    state["mi_stats_last_run"] = mi_stats

    # Persist Binance snapshot summary for the dashboard.
    # Always update the key so the dashboard reflects the current cycle's status.
    if binance_snap is not None:
        state["binance_last_run"] = {
            "fetched_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(binance_snap.fetched_at)
            ),
            "assets": {
                asset: {
                    "ticker": sn.ticker,
                    "price": round(sn.current_price, 2),
                    "return_pct": round((sn.recent_return or 0.0) * 100, 3),
                    "vol_pct": round((sn.price_volatility or 0.0) * 100, 3),
                }
                for asset, sn in binance_snap.assets.items()
            },
            "errors": binance_snap.errors,
        }
    else:
        # Binance unavailable this cycle — write an explicit error marker so the
        # dashboard shows a clear "unavailable" state rather than stale data.
        state["binance_last_run"] = {
            "fetch_error": True,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
            "assets": {},
            "errors": {},
        }

    _prune_state(state)
    save_state(state)
    write_dashboard(state)

    end_ts = utc_now()
    ms = state.get("market_scan") if isinstance(state.get("market_scan"), dict) else {}
    total_seen = ms.get("total_seen")
    total_kept = ms.get("total_kept")
    n_positions = len(state.get("positions") or {}) if isinstance(state.get("positions"), dict) else 0
    n_trades = len(state.get("trades") or []) if isinstance(state.get("trades"), list) else 0
    mi_summary = " ".join(f"{k}={v}" for k, v in sorted(mi_stats.items()))
    binance_str = ""
    if binance_snap is not None and binance_snap.assets:
        parts = []
        for asset, sn in sorted(binance_snap.assets.items()):
            ret = sn.recent_return
            sign = "+" if (ret or 0.0) >= 0 else ""
            parts.append(f"{asset}=${sn.current_price:.0f}({sign}{(ret or 0.0)*100:.1f}%)")
        binance_str = " binance=[" + " ".join(parts) + "]"
    print(
        f"[{end_ts}] trader: done market_scan(seen={total_seen}, kept={total_kept}) "
        f"positions={n_positions} trades={n_trades} mi=[{mi_summary}]{binance_str}",
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
