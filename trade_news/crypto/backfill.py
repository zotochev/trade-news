"""One-off: the last ~30 days (29) of 5-minute history that Binance still serves (it keeps no more).

/futures/data/openInterestHist (sumOpenInterest, sumOpenInterestValue) and
/futures/data/globalLongShortAccountRatio, period 5m, 500 points per request, walked by
startTime. Funding is left empty (the settled history has another meaning than the live
rate). Rows go to crypto_derivs with insert-or-ignore, so live snapshots are never replaced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog

from trade_news.collectors.base import Getter
from trade_news.collectors.crypto_leverage import BINANCE
from trade_news.db.engine import insert_ignore_many
from trade_news.db.schema import crypto_derivs

log = structlog.get_logger()
PAGE = 500
MAX_DAYS = 29  # "the last 30 days", but a start exactly 30 days back is rejected (400)
STEP_MS = 5 * 60 * 1000


def _series(get: Getter, path: str, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out, t = [], start_ms
    while t < end_ms:
        page = get(f"{BINANCE}{path}", params={"symbol": symbol, "period": "5m", "limit": PAGE,
                   "startTime": t, "endTime": min(t + PAGE * STEP_MS, end_ms)}).json()  # fmt: skip
        if not isinstance(page, list):
            raise ValueError(f"unexpected Binance answer: {str(page)[:200]}")
        out += page
        t += PAGE * STEP_MS
    return out


def backfill(
    engine: sa.Engine, get: Getter, symbols: list[str], now: datetime, days: int = 30
) -> int:
    end = int(now.timestamp() * 1000)
    start = int((now - timedelta(days=min(days, MAX_DAYS))).timestamp() * 1000)
    total = 0
    for base in symbols:
        sym = f"{base}USDT"
        try:
            oi = _series(get, "/futures/data/openInterestHist", sym, start, end)
            ls = {r["timestamp"]: r for r in _series(
                get, "/futures/data/globalLongShortAccountRatio", sym, start, end)}  # fmt: skip
        except Exception as exc:
            log.warning("crypto_backfill_failed", symbol=sym, error=repr(exc))
            continue
        rows = []
        for r in oi:
            coins, usd = float(r["sumOpenInterest"]), float(r["sumOpenInterestValue"])
            ratio = ls.get(r["timestamp"], {}).get("longShortRatio")
            rows.append({
                "exchange": "binance", "symbol": base,
                "ts": datetime.fromtimestamp(r["timestamp"] / 1000, UTC),
                "oi_usd": usd, "oi_coins": coins, "funding_rate": None, "funding_interval_h": None,
                "long_short_ratio": float(ratio) if ratio else None,
                "mark_price": usd / coins if coins else None,
            })  # fmt: skip
        with engine.begin() as conn:
            n = insert_ignore_many(conn, crypto_derivs, rows, "symbol", "exchange", "ts")
        log.info("crypto_backfill_symbol", symbol=base, points=len(rows), new=n)
        total += n
    return total
