"""Market scan for Polymarket Gamma.

Goal: produce a concise, liquidity-filtered list for the dashboard.

Filters (from user):
- all markets
- spread <= 0.03
- endDate within next 30 days
- not resolved

Notes:
- Gamma fields can vary. We use best-effort parsing.
- Spread is derived from bestBid/bestAsk (top-of-book). If missing, we skip
  for the "liquidity" filtered list.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from polymarket_client import GammaClient, get_spread


def _parse_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _parse_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _parse_end_ts(m: Dict[str, Any]) -> Optional[float]:
    """Parse market end date to a UTC epoch float.

    Uses calendar.timegm (not time.mktime) so the result is always UTC-correct
    regardless of the local timezone of the machine.
    """
    # Prefer endDate (ISO-8601 with Z), fallback to end_date
    s = m.get("endDate") or m.get("end_date") or m.get("end")
    if not s:
        return None
    # Handle numeric timestamps
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str):
        return None
    try:
        # Expect: 2026-03-08T17:00:00Z  — treat as UTC via calendar.timegm
        if s.endswith("Z"):
            return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
        # Has timezone offset or bare datetime — parse first 19 chars as UTC (best effort)
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return None


def _is_resolved(m: Dict[str, Any]) -> bool:
    # Gamma often has 'resolved' bool; sometimes 'status'
    if "resolved" in m:
        return bool(m.get("resolved"))
    status = str(m.get("status") or "").lower()
    if status in ("resolved", "closed", "finalized"):
        return True
    return False


@dataclass
class ScanConfig:
    max_spread: float = 0.03
    days_ahead: int = 30
    pages: int = 20
    page_size: int = 100


def scan_markets(client: GammaClient, cfg: ScanConfig) -> Dict[str, Any]:
    now = time.time()
    horizon = now + cfg.days_ahead * 24 * 3600

    markets_seen: List[Dict[str, Any]] = []
    for i in range(cfg.pages):
        # active=True/closed=False keeps it mostly current
        ms = client.get_markets(limit=cfg.page_size, offset=i * cfg.page_size, active=True, closed=False, order="volume", ascending=False)
        if not ms:
            break
        markets_seen.extend(ms)
        time.sleep(0.15)

    kept: List[Dict[str, Any]] = []
    flagged: List[Tuple[str, str]] = []  # (reason, slug)

    for m in markets_seen:
        slug = str(m.get("slug") or m.get("id") or "")
        if _is_resolved(m):
            continue

        end_ts = _parse_end_ts(m)
        if end_ts is None:
            # Some markets may not have endDate; skip for now
            flagged.append(("missing_end", slug))
            continue
        if end_ts < now:
            continue
        if end_ts > horizon:
            continue

        spread = get_spread(m)
        if spread is None:
            flagged.append(("missing_spread", slug))
            continue
        if spread > cfg.max_spread:
            continue

        kept.append(
            {
                "id": m.get("id"),
                "slug": m.get("slug"),
                "question": m.get("question") or m.get("title"),
                "end": m.get("endDate") or m.get("end_date"),
                "bestBid": _parse_float(m.get("bestBid")),
                "bestAsk": _parse_float(m.get("bestAsk")),
                "spread": spread,
                "volume": _parse_float(m.get("volume")),
                "liquidity": _parse_float(m.get("liquidity")),
            }
        )

    kept.sort(key=lambda x: (x["spread"], -(x.get("volume") or 0.0)))

    # Flag summary counts
    flag_counts: Dict[str, int] = {}
    for reason, _ in flagged:
        flag_counts[reason] = flag_counts.get(reason, 0) + 1

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "filters": {
            "max_spread": cfg.max_spread,
            "days_ahead": cfg.days_ahead,
            "active": True,
            "closed": False,
            "order": "volume",
        },
        "total_seen": len(markets_seen),
        "total_kept": len(kept),
        "flag_counts": flag_counts,
        "top": kept[:25],
    }


if __name__ == "__main__":
    c = GammaClient()
    report = scan_markets(c, ScanConfig())
    import json

    print(json.dumps(report, ensure_ascii=False, indent=2))
