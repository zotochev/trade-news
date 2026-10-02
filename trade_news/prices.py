"""Price bars for charts: Yahoo Finance chart API, fetched on demand, kept only in memory.

Nothing is stored in the database: a chart asks for an asset, one request per asset and
interval returns the whole range (1h: 3 months, 1d: 2 years), and the result stays in a small
in-process cache (the last CACHE_SIZE series) until it is older than the refresh age.

Our symbols are mapped to Yahoo's: US equities as they are (BRK-B stays), indices and futures
by a table, FX pairs as EURUSD=X, crypto as BTC-USD. Assets Yahoo has no series for (macro,
the 2-year yield) return None from yahoo_symbol.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import structlog

log = structlog.get_logger()

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "Mozilla/5.0 (trade-news)"  # Yahoo rejects requests without a browser-like UA
INTERVALS = {"1h": ("60m", "3mo", timedelta(minutes=30)), "1d": ("1d", "2y", timedelta(hours=6))}

YAHOO = {
    # indices
    "SPX": "^GSPC", "NDX": "^NDX", "DJI": "^DJI", "RUT": "^RUT", "SOX": "^SOX", "VIX": "^VIX",
    "DXY": "DX-Y.NYB", "DAX": "^GDAXI", "FTSE": "^FTSE", "STOXX50": "^STOXX50E",
    "NIKKEI": "^N225", "HSI": "^HSI",
    # commodities (front-month futures)
    "XAUUSD": "GC=F", "XAGUSD": "SI=F", "WTI": "CL=F", "BRENT": "BZ=F", "NATGAS": "NG=F",
    "COPPER": "HG=F", "PLATINUM": "PL=F", "WHEAT": "ZW=F", "CORN": "ZC=F",
    # Treasury yields (CBOE, in percent)
    "US10Y": "^TNX", "US30Y": "^TYX",
}  # fmt: skip


def yahoo_symbol(asset_class: str, symbol: str) -> str | None:
    if symbol in YAHOO:
        return YAHOO[symbol]
    if asset_class == "equity":
        return symbol.replace(".", "-")
    if asset_class == "fx" and len(symbol) == 6:
        return f"{symbol}=X"
    if asset_class == "crypto" and symbol not in ("USDT", "USDC"):
        return f"{symbol}-USD"
    return None


def parse_chart(data: dict) -> list[dict]:
    """Yahoo chart JSON → [{ts, open, high, low, close, volume}]; bars without a close skipped."""
    result = ((data.get("chart") or {}).get("result") or [None])[0] or {}
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]

    def column(name: str) -> list:
        return quote.get(name) or [None] * len(stamps)

    cols = {k: column(k) for k in ("open", "high", "low", "close", "volume")}
    out = []
    for i, t in enumerate(stamps):
        if cols["close"][i] is None:
            continue
        out.append({"ts": datetime.fromtimestamp(t, UTC), **{k: v[i] for k, v in cols.items()}})
    # The last element is often the live quote, not a bar: its time is off the bars' grid
    # (16:32:15 after bars at :30:00). It would break the candle grid, so it's dropped.
    if len(out) > 1:
        last, prev = out[-1]["ts"], out[-2]["ts"]
        if (last.minute, last.second) != (prev.minute, prev.second):
            out.pop()
    return out


def fetch_bars(http: httpx.Client, yahoo: str, interval: str) -> list[dict]:
    yahoo_interval, rng, _ = INTERVALS[interval]
    resp = http.get(
        CHART_URL.format(symbol=yahoo),
        params={"interval": yahoo_interval, "range": rng, "includePrePost": "false"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return parse_chart(resp.json())


CACHE_SIZE = 50
_cache: OrderedDict[tuple[str, str], tuple[datetime, list[dict]]] = OrderedDict()
_lock = threading.Lock()


def get_bars(
    yahoo: str, interval: str, now: datetime, fetch: Callable[[str, str], list[dict]]
) -> list[dict]:
    """Bars from the cache, or fetched when missing or older than the refresh age. A failed
    fetch serves the stale copy if there is one (logged)."""
    _, _, refresh_after = INTERVALS[interval]
    key = (yahoo, interval)
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < refresh_after:
            _cache.move_to_end(key)
            return cached[1]
    try:
        bars = fetch(yahoo, interval)
    except httpx.HTTPError as exc:
        log.warning("prices_fetch_failed", symbol=yahoo, interval=interval, error=repr(exc))
        return cached[1] if cached else []
    with _lock:
        _cache[key] = (now, bars)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return bars


def clear_cache() -> None:
    with _lock:
        _cache.clear()
