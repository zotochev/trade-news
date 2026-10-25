"""Data for the admin "Крипта" page: everything we collect about one coin on one screen.

Grouped by the crypto model's chain «причины → участники → стакан → цена»:
- participants: leverage (open interest, funding, long/short per exchange, crypto_derivs),
  spot ETF flows (etf_flows), public company holdings (crypto_treasuries);
- order book: depth near mid (crypto_orderbook) and forced flow (crypto_liquidations);
- causes: token unlocks ahead (token_unlocks) and the latest annotated crypto news;
- the state of the collectors that feed the page.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

import sqlalchemy as sa

from trade_news.crypto.history import funding_8h
from trade_news.db.schema import (
    crypto_derivs,
    crypto_liquidations,
    crypto_orderbook,
    crypto_treasuries,
    etf_flows,
    token_unlocks,
)

ASSETS = ("BTC", "ETH")
EXCHANGES = ("binance", "bybit", "okx")
VENUES = {
    "binance_spot": "Binance спот",
    "binance_perp": "Binance перп",
    "bybit_perp": "Bybit перп",
    "okx_perp": "OKX перп",
}
PERP_SET = ("binance_perp", "okx_perp")  # both always reach ±0.1 %: a stable sum
SOURCES = ("crypto_leverage", "crypto_orderbook", "etf_flows", "crypto_treasuries",
           "token_unlocks", "cointelegraph", "decrypt", "theblock")  # fmt: skip


def money(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "—"
    s = "+" if sign and v > 0 else ("−" if v < 0 else "")
    a = abs(v)
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{s}${a / div:.{1 if a < div * 100 else 0}f}{unit}"
    return f"{s}${a:.0f}"


def spark(values: list[float | None], w: int = 260, h: int = 48) -> str:
    """SVG path of a line over `values` (None: a gap), scaled to the box."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span, n = (hi - lo) or 1, max(len(values) - 1, 1)
    out, prev = [], None
    for i, v in pts:
        x, y = i * w / n, h - 2 - (v - lo) / span * (h - 4)
        out.append(f"{'M' if prev is None or i - prev > 1 else 'L'}{x:.1f},{y:.1f}")
        prev = i
    return " ".join(out)


# --- leverage ---------------------------------------------------------------------------


@dataclass
class Leverage:
    at: datetime | None = None
    rows: list[dict] = field(default_factory=list)  # per exchange, latest
    oi: float | None = None
    oi_change: float | None = None  # vs 24 h ago, fraction
    funding: float | None = None  # 8h, OI-weighted
    ls: float | None = None  # mean of the exchanges' account ratios
    price: float | None = None
    price_change: float | None = None
    series: dict[str, str] = field(default_factory=dict)  # spark paths over 7 days
    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)


def _snap(rows: list[dict]) -> dict:
    with_oi = [r for r in rows if r["oi_usd"]]
    oi = sum(r["oi_usd"] for r in with_oi) or None
    fund = [(funding_8h(r["funding_rate"], r["funding_interval_h"]), r["oi_usd"]) for r in with_oi]
    fund = [(f, w) for f, w in fund if f is not None]
    marks = [r["mark_price"] for r in rows if r["mark_price"]]
    ls = [r["long_short_ratio"] for r in rows if r["long_short_ratio"]]
    return {
        "oi": oi,
        "funding": sum(f * w for f, w in fund) / sum(w for _, w in fund) if fund else None,
        "price": median(marks) if marks else None,
        "ls": sum(ls) / len(ls) if ls else None,
        "n": len(rows),
    }


def leverage(conn, asset: str, now: datetime) -> Leverage:
    c = crypto_derivs.c
    rows = conn.execute(
        sa.select(crypto_derivs)
        .where(c.symbol == asset, c.ts >= now - timedelta(days=7, minutes=10))
        .order_by(c.ts)
    ).mappings()
    by_ts: dict[datetime, list[dict]] = defaultdict(list)
    for r in rows:
        by_ts[r["ts"]].append(dict(r))
    out = Leverage()
    if not by_ts:
        return out
    full = max(len(v) for v in by_ts.values())
    stamps = [t for t in sorted(by_ts) if len(by_ts[t]) == full]  # only complete snapshots
    last = stamps[-1] if stamps else max(by_ts)
    out.at = last
    out.rows = sorted(
        (r | {"funding_8h": funding_8h(r["funding_rate"], r["funding_interval_h"])}
         for r in by_ts[last]),
        key=lambda r: EXCHANGES.index(r["exchange"]) if r["exchange"] in EXCHANGES else 9,
    )  # fmt: skip
    now_s = _snap(by_ts[last])
    out.oi, out.funding, out.ls = now_s["oi"], now_s["funding"], now_s["ls"]
    out.price = now_s["price"]
    day_ago = [t for t in stamps if t <= last - timedelta(hours=24)]
    if day_ago:
        before = _snap(by_ts[day_ago[-1]])
        if before["oi"] and out.oi:
            out.oi_change = out.oi / before["oi"] - 1
        if before["price"] and out.price:
            out.price_change = out.price / before["price"] - 1
    hourly = [t for t in stamps if t.minute == 0]
    snaps = [_snap(by_ts[t]) for t in hourly]
    for key in ("price", "oi", "funding"):
        vals = [s[key] for s in snaps]
        out.series[key] = spark(vals)
        known = [v for v in vals if v is not None]
        if known:
            out.ranges[key] = (min(known), max(known))
    return out


# --- liquidations -----------------------------------------------------------------------


def liquidations(conn, asset: str, now: datetime) -> dict:
    c = crypto_liquidations.c
    rows = conn.execute(
        sa.select(c.exchange, c.minute, c.side, c.usd).where(
            c.symbol == asset, c.minute >= now - timedelta(hours=24)
        )
    ).all()
    windows = {"1 ч": 1, "4 ч": 4, "24 ч": 24}
    sums = {k: {"long": 0.0, "short": 0.0} for k in windows}
    by_ex: dict[str, dict[str, float]] = {ex: {"long": 0.0, "short": 0.0} for ex in EXCHANGES}
    hours: dict[int, dict[str, float]] = defaultdict(lambda: {"long": 0.0, "short": 0.0})
    for r in rows:
        age = (now - r.minute).total_seconds() / 3600
        for k, h in windows.items():
            if age <= h:
                sums[k][r.side] += r.usd
        by_ex.setdefault(r.exchange, {"long": 0.0, "short": 0.0})[r.side] += r.usd
        hours[min(int(age), 23)][r.side] += r.usd
    bars = [hours[h] for h in range(23, -1, -1)]  # oldest first
    top = max((b["long"] + b["short"] for b in bars), default=0) or 1
    return {
        "sums": sums,
        "by_ex": by_ex,
        "bars": [
            b | {"lp": b["long"] / top * 100, "sp": b["short"] / top * 100, "hour": 23 - i}
            for i, b in enumerate(bars)
        ],
        "max_hour": top if rows else 0,
    }


# --- order book -------------------------------------------------------------------------


def orderbook(conn, asset: str, now: datetime) -> dict:
    c = crypto_orderbook.c
    rows = [
        dict(r)
        for r in conn.execute(
            sa.select(crypto_orderbook)
            .where(c.symbol == asset, c.ts >= now - timedelta(hours=24))
            .order_by(c.ts)
        ).mappings()
    ]
    if not rows:
        return {"at": None, "rows": [], "spot": None, "perp": None}
    last = rows[-1]["ts"]
    latest = {r["venue"]: r for r in rows if r["ts"] == last}

    def both(r: dict | None, band: str = "01") -> float | None:
        if not r or r[f"bid_{band}"] is None or r[f"ask_{band}"] is None:
            return None
        return r[f"bid_{band}"] + r[f"ask_{band}"]

    def perp(venues: dict) -> float | None:
        parts = [both(venues.get(v)) for v in PERP_SET]
        return sum(parts) if all(p is not None for p in parts) else None

    by_ts: dict[datetime, dict] = defaultdict(dict)
    for r in rows:
        by_ts[r["ts"]][r["venue"]] = r
    spot_day = [x for v in by_ts.values() if (x := both(v.get("binance_spot"))) is not None]
    perp_day = [x for v in by_ts.values() if (x := perp(v)) is not None]
    table = []
    for venue, name in VENUES.items():
        r = latest.get(venue)
        if not r:
            continue
        imb = None
        if r["bid_01"] is not None and r["ask_01"] is not None and r["bid_01"] + r["ask_01"]:
            imb = r["bid_01"] / (r["bid_01"] + r["ask_01"])
        table.append(r | {"name": name, "imbalance": imb})
    return {
        "at": last,
        "rows": table,
        "spot": both(latest.get("binance_spot")),
        "spot_median": median(spot_day) if spot_day else None,
        "perp": perp(latest),
        "perp_median": median(perp_day) if perp_day else None,
    }


# --- flows ------------------------------------------------------------------------------


def etf(conn, asset: str, days: int = 10) -> dict:
    e = etf_flows.c
    rows = conn.execute(
        sa.select(e.trade_date, sa.func.sum(e.flow_usd_m).label("m"))
        .where(e.asset == asset)
        .group_by(e.trade_date)
        .order_by(e.trade_date.desc())
        .limit(days)
    ).all()
    out = [{"date": r.trade_date, "usd": r.m * 1e6} for r in reversed(rows)]
    top = max((abs(r["usd"]) for r in out), default=0) or 1
    for r in out:
        r["pct"] = abs(r["usd"]) / top * 100
    return {
        "days": out,
        "last": out[-1] if out else None,
        "sum5": sum(r["usd"] for r in out[-5:]) if out else None,
    }


def treasuries(conn, coin: str, limit: int = 8) -> dict:
    t = crypto_treasuries.c
    dates = conn.execute(
        sa.select(t.snapshot_date).where(t.coin == coin).group_by(t.snapshot_date)
        .order_by(t.snapshot_date.desc())
    ).scalars().all()  # fmt: skip
    if not dates:
        return {"at": None, "rows": [], "since": None, "total": None, "total_change": None}
    last = dates[0]
    base = next((d for d in reversed(dates) if d >= last - timedelta(days=30)), last)
    now_rows = conn.execute(
        sa.select(crypto_treasuries).where(t.coin == coin, t.snapshot_date == last)
        .order_by(t.holdings.desc())
    ).mappings().all()  # fmt: skip
    before = dict(
        conn.execute(
            sa.select(t.name, t.holdings).where(t.coin == coin, t.snapshot_date == base)
        ).all()
    )
    rows = [
        dict(r) | {"change": r["holdings"] - before[r["name"]] if r["name"] in before else None}
        for r in now_rows[:limit]
    ]
    total = sum(r["holdings"] for r in now_rows)
    total_before = sum(before.values()) if base != last else None
    return {
        "at": last,
        "since": base if base != last else None,
        "rows": rows,
        "total": total,
        "total_change": total - total_before if total_before is not None else None,
        "companies": len(now_rows),
    }


def unlocks(conn, now: datetime, days: int = 14, limit: int = 12) -> dict:
    at = token_unlocks.c.unlock_at
    rows = [
        dict(r)
        for r in conn.execute(
            sa.select(token_unlocks).where(at >= now, at < now + timedelta(days=days)).order_by(at)
        ).mappings()
    ]
    week = [r for r in rows if r["unlock_at"] < now + timedelta(days=7)]
    return {"rows": rows[:limit], "total": len(rows), "week": len(week)}


def liquidation_streams(conn, now: datetime) -> list[dict]:
    """The last minute written per exchange: the streams have no collector runs."""
    c = crypto_liquidations.c
    last = dict(
        conn.execute(
            sa.select(c.exchange, sa.func.max(c.minute)).where(c.minute >= now - timedelta(days=30))
            .group_by(c.exchange)
        ).all()
    )  # fmt: skip
    return [{"exchange": ex, "last": last.get(ex)} for ex in EXCHANGES]
