"""Polymarket flow & whale signal layer.

Computes market microstructure signals that capture "smart money" flow:

  Layer 1 — CLOB orderbook (per-market, real-time):
    1a. **Book imbalance**: bid-size vs ask-size from Gamma API metadata.
    1b. **CLOB depth ratio**: full orderbook from CLOB API.

  Layer 2 — PolymarketScan analytics (batch, cached 60s):
    2a. **smart_money_bias**: direction smart money is flowing (blockchain-derived).
    2b. **AI vs Humans divergence**: independent AI probability vs market price.

Output:
  flow_adjustment  ∈ [-max_flow_adj, +max_flow_adj]  (added to p_hat)
    > 0  →  buying pressure / bullish divergence  →  p_hat nudged up
    < 0  →  selling pressure / bearish divergence →  p_hat nudged down

Integration (in trader.py, after Binance blend):
    adj = compute_flow_signal(market, clob_client=client, scan_data=scan)
    p_hat = clamp(p_hat + adj, 0.01, 0.99)

Graceful degradation: returns 0.0 (neutral) when data is unavailable.
No external deps — urllib only (matches project convention).
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


# ── Config ────────────────────────────────────────────────────────────────────

CLOB_BASE_URL = "https://clob.polymarket.com"
SCAN_BASE_URL = "https://gzydspfquuaudqeztorw.supabase.co/functions/v1/agent-api"


@dataclass(frozen=True)
class FlowConfig:
    """Tunable parameters for the flow signal layer."""

    # Maximum absolute adjustment to p_hat from flow signal.
    max_flow_adj: float = 0.04

    # ── Layer 1 weights: CLOB orderbook imbalance ─────────────────────────
    weight_top_of_book: float = 0.20
    weight_clob_depth:  float = 0.25

    # ── Layer 2 weights: PolymarketScan analytics ─────────────────────────
    weight_smart_money: float = 0.25   # smart_money_bias from /markets
    weight_ai_diverge:  float = 0.30   # AI vs Humans divergence

    # Minimum total size (bid+ask combined) to trust the imbalance signal.
    min_total_size: float = 50.0

    # CLOB depth levels to consider (full book is expensive; top N is enough).
    clob_depth_levels: int = 10

    # Cache TTL for CLOB orderbook data (seconds).
    clob_cache_ttl: float = 300.0

    # AI divergence: minimum absolute divergence to consider as a signal.
    # Below this the AI and market roughly agree — no signal.
    ai_diverge_min: float = 5.0   # percentage points


# ── CLOB Client ───────────────────────────────────────────────────────────────

class CLOBError(RuntimeError):
    pass


@dataclass
class CLOBClient:
    """Minimal client for the Polymarket CLOB API (read-only, no auth)."""

    base_url: str = CLOB_BASE_URL
    timeout_s: int = 10
    user_agent: str = "polymarket-paper/0.1"

    # Simple in-memory cache: token_id → (timestamp, data)
    _cache: Dict[str, Tuple[float, Any]] = field(
        default_factory=dict, repr=False
    )

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: str(v) for k, v in params.items()}
            )

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as e:
            raise CLOBError(f"GET {url} failed: {e}") from e

    def get_orderbook(
        self, token_id: str, *, cache_ttl: float = 300.0
    ) -> Optional[Dict[str, Any]]:
        """Fetch orderbook summary for a token.

        Returns dict with 'bids' and 'asks' lists, or None on error.
        Each bid/ask is {"price": "0.55", "size": "120.5"}.

        Uses a simple in-memory cache to avoid hammering the API.
        """
        now = time.time()
        cached = self._cache.get(token_id)
        if cached is not None:
            ts, data = cached
            if (now - ts) < cache_ttl:
                return data

        try:
            data = self._get(f"/orderbook/{token_id}")
            self._cache[token_id] = (now, data)
            return data
        except CLOBError:
            return None

    def get_last_trade(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Fetch last trade price and side for a token.

        Returns dict with 'price' and 'side' (BUY/SELL), or None on error.
        """
        try:
            return self._get(f"/last-trade-price/{token_id}")
        except CLOBError:
            return None


# ── PolymarketScan Client ─────────────────────────────────────────────────────

class ScanAPIError(RuntimeError):
    pass


@dataclass
class ScanData:
    """Cached batch of PolymarketScan analytics for the current cycle.

    Fetched once per cycle (not per-market) to stay within rate limits.
    Provides smart_money_bias per market and AI-vs-humans divergence data.
    """

    # slug → market dict from /markets (includes smart_money_bias, whale_count)
    markets_by_slug: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # slug → divergence dict from /ai-vs-humans
    ai_diverge_by_slug: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    fetched_at: float = 0.0

    @classmethod
    def empty(cls) -> "ScanData":
        return cls(fetched_at=time.time())

    def get_smart_money_bias(self, slug: str) -> Optional[float]:
        """Return smart_money_bias for a market slug, or None."""
        m = self.markets_by_slug.get(slug)
        if m is None:
            return None
        v = m.get("smart_money_bias")
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    def get_ai_divergence(self, slug: str) -> Optional[Dict[str, Any]]:
        """Return AI divergence record for a market slug, or None."""
        return self.ai_diverge_by_slug.get(slug)


def _scan_get(params: Dict[str, Any], timeout: int = 15) -> Any:
    """Low-level GET to the PolymarketScan Agent API."""
    url = SCAN_BASE_URL + "?" + urllib.parse.urlencode(
        {k: str(v) for k, v in params.items()}
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "polymarket-paper/0.1",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            body = json.loads(raw)
            if not body.get("ok"):
                raise ScanAPIError(f"API returned ok=false: {body}")
            return body.get("data", [])
    except ScanAPIError:
        raise
    except Exception as e:
        raise ScanAPIError(f"GET {url} failed: {e}") from e


def fetch_scan_data() -> ScanData:
    """Fetch batch analytics from PolymarketScan once per cycle.

    Makes 2 API calls:
      1) /markets?category=Crypto — smart_money_bias, whale_count
      2) /ai-vs-humans           — AI consensus vs market price

    Returns ScanData with lookup dicts keyed by market slug.
    Errors are non-fatal: returns partial data or empty ScanData.
    """
    sd = ScanData(fetched_at=time.time())

    # 1) Crypto markets with smart money data
    try:
        markets = _scan_get({
            "action": "markets",
            "category": "Crypto",
            "limit": 100,
            "sort": "volume_usd",
        })
        if isinstance(markets, list):
            for m in markets:
                slug = m.get("slug")
                if slug:
                    sd.markets_by_slug[str(slug)] = m
    except ScanAPIError as e:
        print(f"[scan] markets fetch failed: {e}", flush=True)

    # 2) AI vs Humans divergence
    try:
        divergences = _scan_get({
            "action": "ai-vs-humans",
            "limit": 100,
        })
        if isinstance(divergences, list):
            for d in divergences:
                slug = d.get("slug")
                if slug:
                    sd.ai_diverge_by_slug[str(slug)] = d
    except ScanAPIError as e:
        print(f"[scan] ai-vs-humans fetch failed: {e}", flush=True)

    return sd


# ── Signal computation ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FlowResult:
    """Return value of compute_flow_signal() — adjustment + component breakdown."""

    adj: float = 0.0
    tob_imb: Optional[float] = None
    clob_imb: Optional[float] = None
    smart_money: Optional[float] = None
    ai_diverge: Optional[float] = None

    @staticmethod
    def neutral() -> "FlowResult":
        return FlowResult()


def _parse_float(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except (ValueError, TypeError):
        return None


def _extract_token_id(market: Dict[str, Any]) -> Optional[str]:
    """Extract the YES-outcome token ID from a Gamma API market dict.

    The Gamma API returns 'clobTokenIds' as a JSON-encoded list of
    [YES_token, NO_token].  We need the YES token for orderbook queries.
    """
    raw = market.get("clobTokenIds")
    if raw is None:
        return None

    ids = raw
    if isinstance(raw, str):
        try:
            ids = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    if isinstance(ids, list) and len(ids) >= 1 and ids[0]:
        return str(ids[0])
    return None


def compute_book_imbalance(market: Dict[str, Any]) -> Optional[float]:
    """Top-of-book imbalance from Gamma API metadata.

    Returns a value in [-1, +1]:
      +1 = all depth is on the bid side (extreme buying pressure)
      -1 = all depth is on the ask side (extreme selling pressure)
       0 = balanced

    Returns None if sizes are unavailable.
    """
    bid_size = _parse_float(market.get("bestBidSize"))
    ask_size = _parse_float(market.get("bestAskSize"))

    if bid_size is None or ask_size is None:
        return None

    total = bid_size + ask_size
    if total <= 0:
        return None

    return (bid_size - ask_size) / total


def compute_clob_depth_imbalance(
    orderbook: Dict[str, Any],
) -> Optional[float]:
    """Orderbook depth imbalance from CLOB API full book.

    Sums total size on each side and returns imbalance in [-1, +1].
    More robust than top-of-book because it considers depth beyond the BBO.
    """
    bids = orderbook.get("bids") or []
    asks = orderbook.get("asks") or []

    if not bids and not asks:
        return None

    total_bid = 0.0
    for level in bids:
        s = _parse_float(level.get("size") if isinstance(level, dict) else None)
        if s is not None:
            total_bid += s

    total_ask = 0.0
    for level in asks:
        s = _parse_float(level.get("size") if isinstance(level, dict) else None)
        if s is not None:
            total_ask += s

    total = total_bid + total_ask
    if total <= 0:
        return None

    return (total_bid - total_ask) / total


def _compute_smart_money_signal(
    market: Dict[str, Any],
    scan_data: Optional[ScanData],
) -> Optional[float]:
    """Extract smart_money_bias for a market from PolymarketScan.

    Returns a value in [-1, +1]:
      > 0 = smart money is bullish on YES
      < 0 = smart money is bearish on YES
    Returns None if unavailable.
    """
    if scan_data is None:
        return None
    slug = market.get("slug")
    if not slug:
        return None
    bias = scan_data.get_smart_money_bias(str(slug))
    if bias is None:
        return None
    # smart_money_bias is already a number; normalize to [-1, 1]
    return max(-1.0, min(1.0, float(bias) / 100.0))


def _compute_ai_divergence_signal(
    market: Dict[str, Any],
    scan_data: Optional[ScanData],
    cfg: FlowConfig,
) -> Optional[float]:
    """Compute signal from AI vs Humans divergence.

    If AI consensus > market price by a significant margin → bullish signal.
    If AI consensus < market price by a significant margin → bearish signal.

    Returns a value in [-1, +1], or None if unavailable.
    """
    if scan_data is None:
        return None
    slug = market.get("slug")
    if not slug:
        return None
    rec = scan_data.get_ai_divergence(str(slug))
    if rec is None:
        return None

    divergence = _parse_float(rec.get("divergence"))
    if divergence is None:
        return None

    # divergence is in percentage points (e.g., 15 = AI is 15pp above market)
    if abs(divergence) < cfg.ai_diverge_min:
        return None  # too small to be a signal

    # Normalize: cap at ±50pp, map to [-1, +1]
    capped = max(-50.0, min(50.0, divergence))
    return capped / 50.0


def compute_flow_signal(
    market: Dict[str, Any],
    *,
    clob_client: Optional[CLOBClient] = None,
    scan_data: Optional[ScanData] = None,
    cfg: FlowConfig = FlowConfig(),
) -> FlowResult:
    """Compute the flow adjustment for p_hat.

    Combines up to 4 signal sources with weighted blend:
      Layer 1a: Top-of-book imbalance  (weight_top_of_book)
      Layer 1b: CLOB depth imbalance   (weight_clob_depth)
      Layer 2a: Smart money bias       (weight_smart_money)
      Layer 2b: AI vs Humans diverge   (weight_ai_diverge)

    Returns FlowResult with:
      adj ∈ [-max_flow_adj, +max_flow_adj]  and per-component values.
      adj > 0  →  net buying pressure / bullish   → nudge p_hat UP
      adj < 0  →  net selling pressure / bearish  → nudge p_hat DOWN
      adj 0.0  →  neutral / insufficient data
    """
    # Collect available signals: list of (weight, value ∈ [-1,+1])
    signals: list[tuple[float, float]] = []

    # 1a) Top-of-book imbalance (from Gamma market metadata)
    tob_imb = compute_book_imbalance(market)
    bid_size = _parse_float(market.get("bestBidSize")) or 0.0
    ask_size = _parse_float(market.get("bestAskSize")) or 0.0
    if (bid_size + ask_size) < cfg.min_total_size:
        tob_imb = None
    if tob_imb is not None:
        signals.append((cfg.weight_top_of_book, tob_imb))

    # 1b) CLOB depth imbalance
    clob_imb: Optional[float] = None
    token_id = _extract_token_id(market)
    if clob_client is not None and token_id is not None:
        book = clob_client.get_orderbook(token_id, cache_ttl=cfg.clob_cache_ttl)
        if book is not None:
            clob_imb = compute_clob_depth_imbalance(book)
            if clob_imb is not None:
                signals.append((cfg.weight_clob_depth, clob_imb))

    # 2a) Smart money bias from PolymarketScan
    sm = _compute_smart_money_signal(market, scan_data)
    if sm is not None:
        signals.append((cfg.weight_smart_money, sm))

    # 2b) AI vs Humans divergence from PolymarketScan
    ai = _compute_ai_divergence_signal(market, scan_data, cfg)
    if ai is not None:
        signals.append((cfg.weight_ai_diverge, ai))

    if not signals:
        return FlowResult(adj=0.0, tob_imb=tob_imb, clob_imb=clob_imb,
                          smart_money=sm, ai_diverge=ai)

    # Weighted average of available signals, re-normalized by actual weights
    total_weight = sum(w for w, _ in signals)
    if total_weight <= 0:
        return FlowResult(adj=0.0, tob_imb=tob_imb, clob_imb=clob_imb,
                          smart_money=sm, ai_diverge=ai)
    raw = sum(w * v for w, v in signals) / total_weight

    # Scale to max adjustment and clamp
    adj = raw * cfg.max_flow_adj
    adj = max(-cfg.max_flow_adj, min(cfg.max_flow_adj, adj))

    return FlowResult(adj=adj, tob_imb=tob_imb, clob_imb=clob_imb,
                      smart_money=sm, ai_diverge=ai)


# ── Snapshot for dashboard / logging ──────────────────────────────────────────

@dataclass
class FlowSnapshot:
    """Per-cycle summary of flow signals for all markets (for logging/dashboard)."""

    signals: Dict[str, Dict[str, Any]]  # market_id → signal details
    fetched_at: float

    @classmethod
    def empty(cls) -> "FlowSnapshot":
        return cls(signals={}, fetched_at=time.time())

    def record(
        self,
        market_id: str,
        flow_adj: float,
        *,
        tob_imb: Optional[float] = None,
        clob_imb: Optional[float] = None,
        smart_money: Optional[float] = None,
        ai_diverge: Optional[float] = None,
    ) -> None:
        self.signals[market_id] = {
            "flow_adj": round(flow_adj, 5),
            "tob_imbalance": round(tob_imb, 4) if tob_imb is not None else None,
            "clob_imbalance": round(clob_imb, 4) if clob_imb is not None else None,
            "smart_money": round(smart_money, 4) if smart_money is not None else None,
            "ai_diverge": round(ai_diverge, 4) if ai_diverge is not None else None,
        }

    def get_adj(self, market_id: str) -> float:
        """Get flow adjustment for a market, defaulting to 0.0."""
        entry = self.signals.get(market_id)
        if entry is None:
            return 0.0
        return float(entry.get("flow_adj", 0.0))
