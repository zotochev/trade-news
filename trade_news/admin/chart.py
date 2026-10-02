"""Data for the admin "График" page: candles of one asset and its news as chart markers."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta

import sqlalchemy as sa

from trade_news import prices, views
from trade_news.annotation.whale import whale_of
from trade_news.db.schema import annotations, assets, item_assets, items, raw_items


def find_asset(conn, symbol: str) -> dict | None:
    """The asset with this symbol; across classes the one most linked from news."""
    mentions = sa.func.count(item_assets.c.id)
    row = conn.execute(
        sa.select(assets.c.id, assets.c.asset_class, assets.c.symbol, assets.c.name)
        .outerjoin(item_assets, item_assets.c.asset_id == assets.c.id)
        .where(sa.func.upper(assets.c.symbol) == symbol.strip().upper())
        .group_by(assets.c.id)
        .order_by(mentions.desc())
        .limit(1)
    ).first()
    return dict(row._mapping) if row else None


def _mention_query(since: datetime):
    """Assets linked from news since `since`: item count and bullish / bearish links."""
    mentions = sa.func.count(sa.distinct(item_assets.c.item_id))
    bull = sa.func.sum(sa.case((item_assets.c.direction == "bullish", 1), else_=0))
    bear = sa.func.sum(sa.case((item_assets.c.direction == "bearish", 1), else_=0))
    return (
        sa.select(
            assets.c.asset_class, assets.c.symbol, assets.c.name, mentions.label("n"),
            bull.label("bull"), bear.label("bear"),
        )
        .join(item_assets, item_assets.c.asset_id == assets.c.id)
        .join(items, items.c.id == item_assets.c.item_id)
        .where(
            items.c.published_at >= since,
            item_assets.c.annotation_id.in_(views.latest_annotation_ids()),
        )
        .group_by(assets.c.id)
        .order_by(mentions.desc(), assets.c.symbol)
    )  # fmt: skip


def _chartable(rows, limit: int) -> list[dict]:
    out = [dict(r._mapping) for r in rows if prices.yahoo_symbol(r.asset_class, r.symbol)]
    return out[:limit]


def suggestions(conn, now: datetime, limit: int = 60) -> list[dict]:
    """Assets most written about in the last 7 days that we can chart."""
    return _chartable(conn.execute(_mention_query(now - timedelta(days=7)).limit(limit * 2)), limit)


def search(conn, q: str, now: datetime, limit: int = 15) -> list[dict]:
    """Chartable assets with news in the last 30 days whose symbol starts with / name contains
    `q`, most written about first."""
    q = q.strip()
    if not q:
        return []
    cond = sa.or_(
        sa.func.upper(assets.c.symbol).like(f"{q.upper()}%"),
        sa.func.lower(assets.c.name).like(f"%{q.lower()}%"),
    )
    query = _mention_query(now - timedelta(days=30)).where(cond).limit(limit * 2)
    return _chartable(conn.execute(query), limit)


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def asset_news(conn, asset_id: int, since: datetime) -> list[dict]:
    """News linked to the asset (any link of the current annotation), oldest first."""
    rows = conn.execute(
        sa.select(
            items.c.id,
            items.c.published_at,
            items.c.source,
            items.c.title,
            items.c.canonical_url,
            raw_items.c.url.label("raw_url"),
            annotations.c.payload_json,
            item_assets.c.direction,
            item_assets.c.importance,
            item_assets.c.is_primary,
        )
        .join(item_assets, item_assets.c.item_id == items.c.id)
        .join(annotations, annotations.c.id == item_assets.c.annotation_id)
        .join(raw_items, raw_items.c.id == items.c.raw_item_id)
        .where(
            item_assets.c.asset_id == asset_id,
            views.current_annotation(),
            items.c.published_at >= since,
        )
        .order_by(items.c.published_at, items.c.id)
    )
    out, seen = [], set()
    for r in rows:
        if r.id in seen:  # an asset linked twice by one annotation
            continue
        seen.add(r.id)
        payload = r.payload_json or {}
        w = whale_of({"payload_json": payload})
        url = r.raw_url or r.canonical_url
        out.append({
            "id": r.id,
            "time": _ts(r.published_at),
            "published": r.published_at.strftime("%d.%m %H:%M"),
            "source": r.source,
            "title": r.title or "",
            "summary": payload.get("summary") or r.title or "",
            "event_type": payload.get("event_type"),
            "direction": r.direction,
            "importance": r.importance,
            "primary": bool(r.is_primary),
            "whale": {"kind": w["whale_kind"], "strength": w.get("strength")} if w else None,
            "url": url if url and url.startswith(("https://", "http://")) else None,
        })  # fmt: skip
    return out


def snap(news: list[dict], bar_times: list[int]) -> list[dict]:
    """Each news gets the bar it falls in: the last bar opening at or before its time (news
    after the close lands on the session's last bar). News before the first bar is dropped."""
    out = []
    for n in news:
        i = bisect_right(bar_times, n["time"]) - 1
        if i >= 0:
            out.append(n | {"bar_time": bar_times[i]})
    return out


def chart_data(conn, symbol: str, interval: str, now: datetime, fetch) -> dict:
    """{asset, interval, yahoo, bars, news} or {error}."""
    asset = find_asset(conn, symbol)
    if asset is None:
        return {"error": f"актив {symbol} не найден"}
    yahoo = prices.yahoo_symbol(asset["asset_class"], asset["symbol"])
    if yahoo is None:
        return {"error": f"для {asset['symbol']} нет источника цен"}
    bars = [b for b in prices.get_bars(yahoo, interval, now, fetch) if b["open"] is not None]
    if not bars:
        return {"error": f"Yahoo не отдал цены для {yahoo}"}
    bar_times = [_ts(b["ts"]) for b in bars]
    return {
        "asset": asset,
        "interval": interval,
        "yahoo": yahoo,
        "bars": [
            {"time": t, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]}
            for t, b in zip(bar_times, bars, strict=True)
        ],
        "news": snap(asset_news(conn, asset["id"], bars[0]["ts"]), bar_times),
    }
