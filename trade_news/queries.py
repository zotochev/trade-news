"""Target queries from the spec (section 3). All return SQLAlchemy selects over indexed columns.

1. asset_news:     all news for an asset (AAPL) over a period
2. class_on_day:   everything applicable to an asset class (fx) on a given day
3. asset_upcoming: what's ahead for an asset (EUR/USD) in the next N days, by importance
4. scheduled_future: all scheduled events in the future (basis for reminders later)

"Applicable on day D" = the relevance interval [relevant_from, coalesce(relevant_to,
relevant_from)] intersects D.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import sqlalchemy as sa

from trade_news.db.schema import assets, item_assets, item_relevance, items
from trade_news.views import latest_annotation_ids

_COLS = (
    items.c.id,
    items.c.published_at,
    items.c.title,
    items.c.canonical_url,
    item_assets.c.asset_class,
    item_assets.c.scope,
    item_assets.c.direction,
    item_assets.c.importance,
    item_relevance.c.relevance_type,
    item_relevance.c.relevant_from,
    item_relevance.c.relevant_to,
)


def _base():
    return (
        sa.select(*_COLS, assets.c.symbol)
        .select_from(items)
        .join(
            item_assets,
            sa.and_(
                item_assets.c.item_id == items.c.id,
                item_assets.c.annotation_id.in_(latest_annotation_ids()),
            ),
        )
        .join(
            item_relevance,
            sa.and_(
                item_relevance.c.item_id == items.c.id,
                item_relevance.c.annotation_id.in_(latest_annotation_ids()),
            ),
        )
        .outerjoin(assets, assets.c.id == item_assets.c.asset_id)
    )


def _asset_ids(symbol: str):
    return sa.select(assets.c.id).where(assets.c.symbol == symbol.upper().replace("/", ""))


def _overlaps(start: datetime, end: datetime):
    rel_end = sa.func.coalesce(item_relevance.c.relevant_to, item_relevance.c.relevant_from)
    return sa.and_(item_relevance.c.relevant_from < end, rel_end >= start)


def asset_news(symbol: str, start: datetime, end: datetime):
    return (
        _base()
        .where(
            item_assets.c.asset_id.in_(_asset_ids(symbol)), items.c.published_at.between(start, end)
        )
        .order_by(items.c.published_at.desc())
    )


def class_on_day(asset_class: str, day: date):
    start = datetime.combine(day, time(), UTC)
    return (
        _base()
        .where(
            item_assets.c.asset_class == asset_class, _overlaps(start, start + timedelta(days=1))
        )
        .order_by(item_assets.c.importance.desc(), item_relevance.c.relevant_from)
    )


def asset_upcoming(symbol: str, now: datetime, days: int = 14):
    return (
        _base()
        .where(
            item_assets.c.asset_id.in_(_asset_ids(symbol)),
            _overlaps(now, now + timedelta(days=days)),
        )
        .order_by(item_assets.c.importance.desc(), item_relevance.c.relevant_from)
    )


def scheduled_future(now: datetime):
    return (
        _base()
        .where(
            item_relevance.c.relevance_type == "scheduled", item_relevance.c.relevant_from >= now
        )
        .order_by(item_relevance.c.relevant_from)
    )
