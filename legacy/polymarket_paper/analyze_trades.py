"""Analyze paper trading results from paper_state.json.

No external deps.

Outputs:
- total trades
- win rate
- avg entry/exit edge
- avg pnl per trade
- win rate by side
- optional edge buckets

Phase-1 additions:
- trades by exit_reason
- avg hold_minutes
- avg entry_spread / exit_spread
- PnL by exit_reason
- PnL by entry_spread buckets
- last closed trades (compact table with key Phase-1 fields)

Usage:
  python3 analyze_trades.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Tuple, Iterable


STATE_PATH = "paper_state.json"


BUCKETS: List[Tuple[float, float | None]] = [
    (0.05, 0.07),
    (0.07, 0.10),
    (0.10, 0.15),
    (0.15, None),
]


def load_state() -> Dict[str, Any]:
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def snum(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        return str(x)
    except Exception:
        return default


def _vals(trades: Iterable[Dict[str, Any]], key: str) -> List[float]:
    return [fnum(t.get(key), 0.0) for t in trades if t.get(key) is not None]


def bucket_for_edge(e: float) -> str:
    a = abs(e)
    for lo, hi in BUCKETS:
        if hi is None:
            if a >= lo:
                return f">={lo:.2f}"
        else:
            if lo <= a < hi:
                return f"{lo:.2f}-{hi:.2f}"
    return "<0.05"


SPREAD_BUCKETS: List[Tuple[float, float | None]] = [
    (0.00, 0.01),
    (0.01, 0.02),
    (0.02, 0.03),
    (0.03, None),
]

QUALITY_BUCKETS: List[Tuple[float, float | None]] = [
    (0.0, 40.0),
    (40.0, 60.0),
    (60.0, 80.0),
    (80.0, 100.000001),
]

PRIORITY_BUCKETS: List[Tuple[float, float | None]] = [
    (0.00, 0.03),
    (0.03, 0.06),
    (0.06, 0.10),
    (0.10, None),
]


def bucket_for_quality(q: float) -> str:
    for lo, hi in QUALITY_BUCKETS:
        if hi is None:
            if q >= lo:
                return f">={lo:.0f}"
        else:
            if lo <= q < hi:
                return f"{lo:.0f}-{min(100.0, hi):.0f}"
    return "?"


def bucket_for_priority(p: float) -> str:
    for lo, hi in PRIORITY_BUCKETS:
        if hi is None:
            if p >= lo:
                return f">{lo:.2f}"
        else:
            if lo <= p < hi:
                return f"{lo:.2f}-{hi:.2f}"
    return "?"


def bucket_for_spread(s: float) -> str:
    for lo, hi in SPREAD_BUCKETS:
        if hi is None:
            if s >= lo:
                return f">={lo:.2f}"
        else:
            if lo <= s < hi:
                return f"{lo:.2f}-{hi:.2f}"
    return "?"


def main() -> int:
    state = load_state()
    trades = list(state.get("trades", []))

    total = len(trades)
    wins = [t for t in trades if fnum(t.get("pnl"), 0.0) > 0]
    win_rate = (len(wins) / total) if total else 0.0

    entry_edges = [fnum(t.get("entry_edge"), 0.0) for t in trades]
    exit_edges = [fnum(t.get("exit_edge"), 0.0) for t in trades]
    pnls = [fnum(t.get("pnl"), 0.0) for t in trades]

    hold_minutes = _vals(trades, "hold_minutes")
    entry_spreads = _vals(trades, "entry_spread")
    exit_spreads = _vals(trades, "exit_spread")

    by_side = defaultdict(list)
    by_reason = defaultdict(list)
    by_spread_bucket = defaultdict(list)
    by_quality_bucket = defaultdict(list)
    by_priority_bucket = defaultdict(list)

    for t in trades:
        side = (t.get("side") or "?").upper()
        by_side[side].append(t)

        r = snum(t.get("exit_reason"), "UNKNOWN") or "UNKNOWN"
        by_reason[r].append(t)

        es = t.get("entry_spread")
        if es is not None:
            by_spread_bucket[bucket_for_spread(fnum(es, 0.0))].append(t)
        else:
            by_spread_bucket["MISSING"].append(t)

        q = t.get("market_quality_score")
        if q is None:
            by_quality_bucket["MISSING"].append(t)
        else:
            by_quality_bucket[bucket_for_quality(fnum(q, 0.0))].append(t)

        pr = t.get("priority_score")
        if pr is None:
            by_priority_bucket["MISSING"].append(t)
        else:
            by_priority_bucket[bucket_for_priority(fnum(pr, 0.0))].append(t)

    print(f"Total trades: {total}")
    print(f"Win rate: {win_rate*100:.1f}% ({len(wins)}/{total})")
    print(f"Avg entry edge: {mean(entry_edges):.4f}")
    print(f"Avg exit edge: {mean(exit_edges):.4f}")
    print(f"Avg PnL/trade: {mean(pnls):.4f}")

    if hold_minutes:
        print(f"Avg hold minutes: {mean(hold_minutes):.2f}")
    else:
        print("Avg hold minutes: n/a (missing in logs)")

    if entry_spreads:
        print(f"Avg entry spread: {mean(entry_spreads):.4f}")
    else:
        print("Avg entry spread: n/a (missing in logs)")

    if exit_spreads:
        print(f"Avg exit spread: {mean(exit_spreads):.4f}")
    else:
        print("Avg exit spread: n/a (missing in logs)")

    for side, ts in sorted(by_side.items()):
        w = sum(1 for t in ts if fnum(t.get("pnl"), 0.0) > 0)
        print(f"Win rate {side}: {(w/len(ts))*100:.1f}% ({w}/{len(ts)})")

    if total:
        print("\nTrades by exit_reason:")
        for r, ts in sorted(by_reason.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            avgp = mean([fnum(t.get("pnl"), 0.0) for t in ts])
            print(f"- {r}: n={len(ts)}, avgPnL={avgp:.4f}, sumPnL={sum(fnum(t.get('pnl'),0.0) for t in ts):.4f}")

        print("\nPnL by entry_spread bucket:")
        for k in ["0.00-0.01", "0.01-0.02", "0.02-0.03", ">=0.03", "MISSING"]:
            ts = by_spread_bucket.get(k, [])
            if not ts:
                continue
            w = sum(1 for t in ts if fnum(t.get("pnl"), 0.0) > 0)
            avgp = mean([fnum(t.get("pnl"), 0.0) for t in ts])
            print(f"- {k}: n={len(ts)}, win%={(w/len(ts))*100:.1f}, avgPnL={avgp:.4f}")

        print("\nPnL by market_quality_score bucket:")
        for k in ["0-40", "40-60", "60-80", "80-100", "MISSING"]:
            ts = by_quality_bucket.get(k, [])
            if not ts:
                continue
            w = sum(1 for t in ts if fnum(t.get("pnl"), 0.0) > 0)
            sump = sum(fnum(t.get("pnl"), 0.0) for t in ts)
            avgp = mean([fnum(t.get("pnl"), 0.0) for t in ts])
            print(f"- {k}: n={len(ts)}, win%={(w/len(ts))*100:.1f}, avgPnL={avgp:.4f}, sumPnL={sump:.4f}")

        print("\nPnL by priority_score bucket:")
        for k in ["0.00-0.03", "0.03-0.06", "0.06-0.10", ">0.10", "MISSING"]:
            ts = by_priority_bucket.get(k, [])
            if not ts:
                continue
            w = sum(1 for t in ts if fnum(t.get("pnl"), 0.0) > 0)
            sump = sum(fnum(t.get("pnl"), 0.0) for t in ts)
            avgp = mean([fnum(t.get("pnl"), 0.0) for t in ts])
            print(f"- {k}: n={len(ts)}, win%={(w/len(ts))*100:.1f}, avgPnL={avgp:.4f}, sumPnL={sump:.4f}")

    # buckets
    buckets = defaultdict(list)
    for t in trades:
        e = fnum(t.get("entry_edge"), 0.0)
        buckets[bucket_for_edge(e)].append(t)

    if total:
        print("\nEdge buckets (by |entry_edge|):")
        for k in ["0.05-0.07", "0.07-0.10", "0.10-0.15", ">=0.15", "<0.05"]:
            ts = buckets.get(k, [])
            if not ts:
                continue
            w = sum(1 for t in ts if fnum(t.get("pnl"), 0.0) > 0)
            avgp = mean([fnum(t.get("pnl"), 0.0) for t in ts])
            print(f"- {k}: n={len(ts)}, win%={(w/len(ts))*100:.1f}, avgPnL={avgp:.4f}")

        # Per-market summary (optional but cheap)
        by_market = defaultdict(list)
        for t in trades:
            mid = snum(t.get("market_id"), "")
            slug = snum(t.get("slug"), "")
            key = slug or mid or (snum(t.get("question"), "")[:40] or "?")
            by_market[key].append(t)

        if by_market:
            print("\nPer-market summary:")
            print("market | n | win% | sumPnL | avgPnL")
            print("-" * 60)
            items = []
            for k, ts in by_market.items():
                n = len(ts)
                w = sum(1 for t in ts if fnum(t.get("pnl"), 0.0) > 0)
                sump = sum(fnum(t.get("pnl"), 0.0) for t in ts)
                avgp = (sump / n) if n else 0.0
                items.append((sump, k, n, w, avgp))
            for sump, k, n, w, avgp in sorted(items, key=lambda x: x[0], reverse=True)[:25]:
                print(f"{k} | {n} | {(w/n)*100:.1f} | {sump:.4f} | {avgp:.4f}")

        # Recent trades table
        print("\nLast closed trades:")
        header = [
            "exit_time",
            "slug",
            "market_id",
            "side",
            "entry_edge",
            "exit_edge",
            "hold_min",
            "entry_spread",
            "exit_spread",
            "exit_reason",
            "pnl",
        ]
        print(" | ".join(header))
        print(" | ".join(["-" * len(h) for h in header]))

        for t in reversed(trades[-15:]):
            mid = snum(t.get("market_id"), "")
            slug = snum(t.get("slug"), "")
            if not slug:
                # fallback to something stable-ish
                slug = mid or snum(t.get("question"), "")[:40] or "?"

            row = [
                snum(t.get("exit_time"), ""),
                slug,
                mid or "?",
                snum((t.get("side") or "?").upper(), "?"),
                f"{fnum(t.get('entry_edge'), 0.0):.3f}",
                f"{fnum(t.get('exit_edge'), 0.0):.3f}",
                f"{fnum(t.get('hold_minutes'), 0.0):.1f}",
                f"{fnum(t.get('entry_spread'), 0.0):.3f}",
                f"{fnum(t.get('exit_spread'), 0.0):.3f}",
                snum(t.get("exit_reason"), ""),
                f"{fnum(t.get('pnl'), 0.0):.4f}",
            ]
            print(" | ".join(row))

    if total:
        print("\nDiagnostic summary:")

        # best quality bucket by sumPnL (fallback avgPnL)
        best_q = None
        for k, ts in by_quality_bucket.items():
            if not ts:
                continue
            sump = sum(fnum(t.get("pnl"), 0.0) for t in ts)
            avgp = mean([fnum(t.get("pnl"), 0.0) for t in ts])
            cand = (sump, avgp, k, len(ts))
            if best_q is None or cand[0] > best_q[0]:
                best_q = cand

        best_p = None
        for k, ts in by_priority_bucket.items():
            if not ts:
                continue
            sump = sum(fnum(t.get("pnl"), 0.0) for t in ts)
            avgp = mean([fnum(t.get("pnl"), 0.0) for t in ts])
            cand = (sump, avgp, k, len(ts))
            if best_p is None or cand[0] > best_p[0]:
                best_p = cand

        # most common exit_reason
        most_reason = None
        for r, ts in by_reason.items():
            cand = (len(ts), r)
            if most_reason is None or cand[0] > most_reason[0]:
                most_reason = cand

        # worst spread bucket by avgPnL (only consider non-empty)
        worst_s = None
        for k, ts in by_spread_bucket.items():
            if not ts:
                continue
            avgp = mean([fnum(t.get("pnl"), 0.0) for t in ts])
            cand = (avgp, k, len(ts))
            if worst_s is None or cand[0] < worst_s[0]:
                worst_s = cand

        if best_q is None:
            print("- Best quality bucket: n/a")
        else:
            sump, avgp, k, n = best_q
            print(f"- Best quality bucket (by sumPnL): {k} (n={n}, sumPnL={sump:.4f}, avgPnL={avgp:.4f})")

        if best_p is None:
            print("- Best priority bucket: n/a")
        else:
            sump, avgp, k, n = best_p
            print(f"- Best priority bucket (by sumPnL): {k} (n={n}, sumPnL={sump:.4f}, avgPnL={avgp:.4f})")

        if most_reason is None:
            print("- Most common exit_reason: n/a")
        else:
            n, r = most_reason
            print(f"- Most common exit_reason: {r} (n={n})")

        if worst_s is None:
            print("- Worst entry_spread bucket (by avgPnL): n/a")
        else:
            avgp, k, n = worst_s
            print(f"- Worst entry_spread bucket (by avgPnL): {k} (n={n}, avgPnL={avgp:.4f})")

        print(f"- Total closed trades: {total}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
