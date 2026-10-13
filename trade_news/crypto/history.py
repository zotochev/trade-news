"""Reading the crypto leverage history for the API: per-exchange snapshots, an all-exchange
total per snapshot and per-minute liquidations."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import sqlalchemy as sa

from trade_news.db.schema import crypto_derivs, crypto_liquidations


def funding_8h(rate: float | None, interval_h: float | None) -> float | None:
    """Funding per 8 hours, so exchanges with 1h / 4h intervals compare."""
    if rate is None:
        return None
    return rate * 8 / (interval_h or 8)


def snapshots(conn, asset: str, start: datetime, end: datetime, exchange: str = "") -> list[dict]:
    c = crypto_derivs.c
    q = sa.select(crypto_derivs).where(c.symbol == asset, c.ts >= start, c.ts <= end)
    if exchange:
        q = q.where(c.exchange == exchange)
    return [
        dict(r) | {"funding_8h": funding_8h(r.funding_rate, r.funding_interval_h)}
        for r in conn.execute(q.order_by(c.ts, c.exchange)).mappings()
    ]


def totals(rows: list[dict]) -> list[dict]:
    """Per snapshot time: summed open interest and OI-weighted 8h funding over exchanges."""
    by_ts: dict[datetime, list[dict]] = defaultdict(list)
    for r in rows:
        by_ts[r["ts"]].append(r)
    out = []
    for ts in sorted(by_ts):
        rs = [r for r in by_ts[ts] if r["oi_usd"]]
        oi = sum(r["oi_usd"] for r in rs)
        weighted = [(r["funding_8h"], r["oi_usd"]) for r in rs if r["funding_8h"] is not None]
        w = sum(o for _, o in weighted)
        out.append({
            "ts": ts,
            "oi_usd": oi or None,
            "funding_8h": sum(f * o for f, o in weighted) / w if w else None,
            "exchanges": len(rs),
        })  # fmt: skip
    return out


def liquidations(
    conn, asset: str, start: datetime, end: datetime, exchange: str = ""
) -> list[dict]:
    c = crypto_liquidations.c
    q = sa.select(crypto_liquidations).where(c.symbol == asset, c.minute >= start, c.minute <= end)
    if exchange:
        q = q.where(c.exchange == exchange)
    return [dict(r) for r in conn.execute(q.order_by(c.minute, c.exchange, c.side)).mappings()]
