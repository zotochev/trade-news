"""Read models and actions behind the admin pages (SQLAlchemy Core, no web code here)."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa

from trade_news.annotation import assets as asset_ref
from trade_news.annotation.contract import PROMPT_VERSION
from trade_news.config import Config
from trade_news.db.engine import insert_ignore
from trade_news.db.schema import (
    annotation_dead_letters,
    annotations,
    asset_aliases,
    asset_resolution_queue,
    assets,
    collector_runs,
    deliveries,
    item_assets,
    item_relevance,
    items,
    llm_calls,
    subscribers,
)
from trade_news.llm.usage import quota_day_start
from trade_news.status import source_statuses
from trade_news.views import (
    EVENT_TYPE,
    SUMMARY,
    current_annotation,
    links_by_item,
    relevance_by_item,
)


def _count(conn, table, *where) -> int:
    return conn.execute(sa.select(sa.func.count()).select_from(table).where(*where)).scalar() or 0


def _annotated():
    return current_annotation()


def primary_importance(conn, since: datetime) -> dict[int, int]:
    """item_id → max importance among its primary links, for items annotated since `since`."""
    q = (
        sa.select(item_assets.c.item_id, sa.func.max(item_assets.c.importance))
        .join(annotations, annotations.c.id == item_assets.c.annotation_id)
        .where(item_assets.c.is_primary.is_(True), annotations.c.created_at >= since, _annotated())
        .group_by(item_assets.c.item_id)
    )
    return dict(conn.execute(q).all())


# --- overview -------------------------------------------------------------------------


def overview(conn, cfg: Config, sources: list[tuple[str, str]], now: datetime, hours: int) -> dict:
    since = now - timedelta(hours=hours)
    r = collector_runs.c
    per_source = {
        row.source: row
        for row in conn.execute(
            sa.select(
                r.source,
                sa.func.count().label("runs"),
                sa.func.sum(sa.case((r.status == "error", 1), else_=0)).label("errors"),
                sa.func.coalesce(sa.func.sum(r.fetched), 0).label("fetched"),
                sa.func.coalesce(sa.func.sum(r.inserted), 0).label("inserted"),
                sa.func.coalesce(sa.func.sum(r.seen_before), 0).label("seen"),
                sa.func.coalesce(sa.func.sum(r.dedup_merged), 0).label("merged"),
            )
            .where(r.started_at >= since)
            .group_by(r.source)
        )
    }
    src_rows = []
    for st in source_statuses(conn, sources, now):
        agg = per_source.get(st.name)
        src_rows.append(
            {
                "status": st,
                "runs": agg.runs if agg else 0,
                "errors": agg.errors if agg else 0,
                "inserted": agg.inserted if agg else 0,
                "seen": agg.seen if agg else 0,
                "merged": agg.merged if agg else 0,
            }
        )
    fetched = sum(a.fetched for a in per_source.values())
    inserted = sum(a.inserted for a in per_source.values())
    merged = sum(a.merged for a in per_source.values())

    imp = primary_importance(conn, since)
    hist = Counter(imp.values())
    specific = _count(
        conn,
        item_assets.join(annotations, annotations.c.id == item_assets.c.annotation_id),
        item_assets.c.scope == "specific",
        annotations.c.created_at >= since,
    )
    unresolved = _count(conn, asset_resolution_queue, asset_resolution_queue.c.created_at >= since)
    return {
        "hours": hours,
        "reset_hour": cfg.llm.quota_reset_hour_utc,
        "fetched": fetched,
        "inserted": inserted,
        "merged": merged,
        "annotated": _count(conn, annotations, annotations.c.created_at >= since),
        "llm_calls": _count(conn, llm_calls, llm_calls.c.started_at >= since),
        "unresolved": unresolved,
        "specific": specific,
        "sent": _count(
            conn, deliveries, deliveries.c.status == "sent", deliveries.c.sent_at >= since
        ),
        "subscribers": _count(conn, subscribers, subscribers.c.is_active.is_(True)),
        "sources": src_rows,
        "recent": recent_important(conn, since, min_importance=3, limit=6),
        "quota": quota(conn, cfg, now),
        "histogram": [{"imp": i, "count": hist.get(i, 0)} for i in range(1, 6)],
        "hist_max": max(hist.values(), default=0),
        "hist_total": len(imp),
    }


def recent_important(conn, since: datetime, min_importance: int, limit: int) -> list[dict]:
    imp = {k: v for k, v in primary_importance(conn, since).items() if v >= min_importance}
    if not imp:
        return []
    rows = conn.execute(
        sa.select(items.c.id, items.c.published_at, SUMMARY.label("summary"))
        .join(annotations, sa.and_(annotations.c.item_id == items.c.id, _annotated()))
        .where(items.c.id.in_(list(imp)))
        .order_by(items.c.published_at.desc())
        .limit(limit)
    ).all()
    links = links_by_item(conn, [r.id for r in rows])
    out = []
    for r in rows:
        primary = next((link for link in links.get(r.id, []) if link["is_primary"]), None)
        out.append(
            {
                "id": r.id,
                "published_at": r.published_at,
                "summary": r.summary,
                "imp": imp[r.id],
                "link": primary,
            }
        )
    return out


def quota(conn, cfg: Config, now: datetime) -> list[dict]:
    day_start = quota_day_start(now, cfg.llm.quota_reset_hour_utc)
    out = []
    for m in cfg.llm.models:
        used = _count(
            conn, llm_calls, llm_calls.c.model == m.name, llm_calls.c.started_at >= day_start
        )
        exhausted = _count(
            conn,
            llm_calls,
            llm_calls.c.model == m.name,
            llm_calls.c.status == "quota_day",
            llm_calls.c.started_at >= day_start,
        )
        out.append(
            {
                "model": m,
                "used": used,
                "pct": min(100.0, 100.0 * used / m.daily_request_limit),
                "exhausted": bool(exhausted),
            }
        )
    return out


# --- news ---------------------------------------------------------------------------


@dataclass
class NewsFilter:
    q: str = ""
    asset_class: str = ""
    min_importance: int = 1
    direction: str = ""
    source: str = ""
    review_only: bool = False
    limit: int = 100


def news(conn, f: NewsFilter) -> list[dict]:
    q = (
        sa.select(
            items.c.id,
            items.c.source,
            items.c.published_at,
            items.c.title,
            items.c.canonical_url,
            SUMMARY.label("summary"),
            EVENT_TYPE.label("event_type"),
        )
        .join(annotations, sa.and_(annotations.c.item_id == items.c.id, _annotated()))
        .order_by(items.c.published_at.desc())
        .limit(f.limit)
    )
    link_cond = [item_assets.c.item_id == items.c.id]
    if f.asset_class:
        link_cond.append(item_assets.c.asset_class == f.asset_class)
    if f.min_importance > 1:
        link_cond.append(item_assets.c.importance >= f.min_importance)
    if f.direction:
        link_cond.append(item_assets.c.direction == f.direction)
    if len(link_cond) > 1:
        q = q.where(sa.exists().where(*link_cond))
    if f.source:
        q = q.where(items.c.source == f.source)
    if f.review_only:
        q = q.where(
            sa.exists().where(
                item_relevance.c.item_id == items.c.id, item_relevance.c.needs_review.is_(True)
            )
        )
    if f.q:
        like = f"%{f.q.strip().lower()}%"
        matching_assets = (
            sa.select(item_assets.c.item_id)
            .join(assets, assets.c.id == item_assets.c.asset_id)
            .where(sa.func.lower(assets.c.symbol) == f.q.strip().lower())
        )
        q = q.where(
            sa.or_(
                sa.func.lower(items.c.title).like(like),
                sa.func.lower(SUMMARY).like(like),
                items.c.id.in_(matching_assets),
            )
        )
    rows = [dict(r._mapping) for r in conn.execute(q)]
    ids = [r["id"] for r in rows]
    links, rel = links_by_item(conn, ids), relevance_by_item(conn, ids)
    for r in rows:
        r["links"] = links.get(r["id"], [])
        r["relevance"] = rel.get(r["id"])
    return rows


def reset_annotation(conn, item_id: int) -> None:
    """Drops the item's current-version annotation so the job annotates it again."""
    for table in (item_assets, item_relevance, asset_resolution_queue):
        conn.execute(table.delete().where(table.c.item_id == item_id))
    conn.execute(
        annotation_dead_letters.delete().where(
            annotation_dead_letters.c.item_id == item_id,
            annotation_dead_letters.c.prompt_version == PROMPT_VERSION,
        )
    )
    conn.execute(annotations.delete().where(annotations.c.item_id == item_id, _annotated()))


# --- LLM ------------------------------------------------------------------------------


def llm_usage(conn, cfg: Config, now: datetime, days: int) -> dict:
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    calls = conn.execute(
        sa.select(llm_calls).where(llm_calls.c.started_at >= since).order_by(llm_calls.c.started_at)
    ).all()
    by_day: dict = {}
    for i in range(days):
        d = (since + timedelta(days=i)).date()
        by_day[d] = {"date": d, "tin": 0, "tout": 0, "calls": 0}
    per_model: dict[str, dict] = defaultdict(lambda: Counter())
    for c in calls:
        day = by_day.get(c.started_at.date())
        if day is not None:
            day["tin"] += c.input_tokens or 0
            day["tout"] += c.output_tokens or 0
            day["calls"] += 1
        m = per_model[c.model]
        m["calls"] += 1
        m["tokens"] += (c.input_tokens or 0) + (c.output_tokens or 0)
        m["cost"] += c.cost_estimate or 0
        m[c.status] += 1
    days_list = list(by_day.values())
    peak = max((d["tin"] + d["tout"] for d in days_list), default=0)

    ann = conn.execute(
        sa.select(
            sa.func.count(),
            sa.func.coalesce(sa.func.sum(annotations.c.input_tokens), 0),
            sa.func.coalesce(sa.func.sum(annotations.c.output_tokens), 0),
            sa.func.coalesce(sa.func.sum(annotations.c.cost_estimate), 0),
        ).where(annotations.c.created_at >= since)
    ).one()
    n_ann = ann[0] or 0
    ok_calls = [c for c in calls if c.status == "ok" and c.n_items]
    return {
        "days": days,
        "by_day": days_list,
        "peak": peak,
        "quota": quota(conn, cfg, now),
        "per_model": {m.name: per_model.get(m.name, Counter()) for m in cfg.llm.models},
        "total_in": sum(d["tin"] for d in days_list),
        "total_out": sum(d["tout"] for d in days_list),
        "total_cost": sum(c.cost_estimate or 0 for c in calls),
        "annotated": n_ann,
        "per_item": {
            "tin": ann[1] / n_ann if n_ann else 0,
            "tout": ann[2] / n_ann if n_ann else 0,
            "cost": ann[3] / n_ann if n_ann else 0,
            "batch": sum(c.n_items for c in ok_calls) / len(ok_calls) if ok_calls else 0,
        },
        "dead_letters": _count(
            conn, annotation_dead_letters, annotation_dead_letters.c.created_at >= since
        ),
        "recent": list(reversed(calls[-20:])),
    }


# --- assets queue -------------------------------------------------------------------


def asset_queue(conn) -> list[dict]:
    q = asset_resolution_queue.c
    rows = conn.execute(
        sa.select(
            q.symbol_or_name,
            q.asset_class,
            sa.func.count().label("n"),
            sa.func.max(q.item_id).label("item_id"),
        )
        .where(q.resolved_at.is_(None))
        .group_by(q.symbol_or_name, q.asset_class)
        .order_by(sa.func.count().desc(), q.symbol_or_name)
    ).all()
    titles = dict(
        conn.execute(
            sa.select(items.c.id, items.c.title).where(items.c.id.in_([r.item_id for r in rows]))
        ).all()
    )
    return [
        {
            "name": r.symbol_or_name,
            "asset_class": r.asset_class,
            "n": r.n,
            "example": titles.get(r.item_id, ""),
        }
        for r in rows
    ]


def asset_counts(conn) -> dict:
    return {"assets": _count(conn, assets), "aliases": _count(conn, asset_aliases)}


def search_assets(conn, text: str, limit: int = 10) -> list[dict]:
    if not text.strip():
        return []
    key = asset_ref.norm(text)
    sym = asset_ref.compact(text)
    alias_hit = sa.select(asset_aliases.c.asset_id).where(
        asset_aliases.c.alias_norm.like(f"{key}%")
    )
    rows = conn.execute(
        sa.select(assets.c.id, assets.c.asset_class, assets.c.symbol, assets.c.name)
        .where(
            sa.or_(
                assets.c.symbol == sym, assets.c.symbol.like(f"{sym}%"), assets.c.id.in_(alias_hit)
            )
        )
        .order_by((assets.c.symbol == sym).desc(), sa.func.length(assets.c.symbol), assets.c.symbol)
        .limit(limit)
    ).all()
    return [dict(r._mapping) for r in rows]


def link_queue(
    conn, name: str, asset_class: str, asset_id: int, save_alias: bool, now: datetime
) -> int:
    """Resolves every queued occurrence of (name, class) to asset_id. Returns links updated."""
    updated = conn.execute(
        item_assets.update()
        .where(
            item_assets.c.raw_symbol == name,
            item_assets.c.asset_class == asset_class,
            item_assets.c.scope == "specific",
            item_assets.c.asset_id.is_(None),
        )
        .values(asset_id=asset_id)
    ).rowcount
    _close_queue(conn, name, asset_class, asset_id, now)
    if save_alias and asset_ref.norm(name):
        insert_ignore(
            conn,
            asset_aliases,
            [
                {
                    "asset_id": asset_id,
                    "alias": name,
                    "alias_norm": asset_ref.norm(name),
                    "lang": asset_ref.NEUTRAL,
                    "source": "admin",
                }
            ],
            "alias",
            "lang",
        )
    return updated


def create_asset(conn, asset_class: str, symbol: str, name: str | None) -> int:
    symbol = symbol.strip().upper()
    (asset_id,) = insert_ignore(
        conn,
        assets,
        [{"asset_class": asset_class, "symbol": symbol, "name": (name or "").strip() or None}],
        "asset_class",
        "symbol",
    )
    if asset_id is None:
        asset_id = conn.execute(
            sa.select(assets.c.id).where(
                assets.c.asset_class == asset_class, assets.c.symbol == symbol
            )
        ).scalar_one()
    for alias in {symbol, (name or "").strip()} - {""}:
        insert_ignore(
            conn,
            asset_aliases,
            [
                {
                    "asset_id": asset_id,
                    "alias": alias,
                    "alias_norm": asset_ref.norm(alias),
                    "lang": asset_ref.NEUTRAL,
                    "source": "admin",
                }
            ],
            "alias",
            "lang",
        )
    return asset_id


def ignore_queue(conn, name: str, asset_class: str, now: datetime) -> None:
    _close_queue(conn, name, asset_class, None, now)


def _close_queue(conn, name, asset_class, asset_id, now) -> None:
    conn.execute(
        asset_resolution_queue.update()
        .where(
            asset_resolution_queue.c.symbol_or_name == name,
            asset_resolution_queue.c.asset_class == asset_class,
            asset_resolution_queue.c.resolved_at.is_(None),
        )
        .values(resolved_asset_id=asset_id, resolved_at=now)
    )


# --- subscribers ------------------------------------------------------------------------


@dataclass
class SubscribersPage:
    rows: list = field(default_factory=list)
    events: list = field(default_factory=list)
    active: int = 0
    received: dict = field(default_factory=dict)


def subscribers_page(conn, now: datetime) -> SubscribersPage:
    rows = conn.execute(
        sa.select(subscribers).order_by(subscribers.c.is_active.desc(), subscribers.c.subscribed_at)
    ).all()
    events = []
    for r in rows:
        events.append({"at": r.subscribed_at, "title": r.title, "what": "подписался"})
        if r.unsubscribed_at:
            what = "заблокировал бота" if r.unsubscribe_reason == "blocked" else "отписался"
            events.append({"at": r.unsubscribed_at, "title": r.title, "what": what})
    events.sort(key=lambda e: e["at"], reverse=True)
    received = dict(
        conn.execute(
            sa.select(deliveries.c.channel, sa.func.count())
            .where(deliveries.c.status == "sent")
            .group_by(deliveries.c.channel)
        ).all()
    )
    return SubscribersPage(
        rows=rows,
        events=events[:15],
        active=sum(1 for r in rows if r.is_active),
        received=received,
    )


# --- deliveries -----------------------------------------------------------------------


def delivery_page(conn, rules, now: datetime) -> dict:
    from trade_news import delivery

    since = now - timedelta(hours=24)
    counts = dict(
        conn.execute(
            sa.select(deliveries.c.status, sa.func.count())
            .where(deliveries.c.created_at >= since)
            .group_by(deliveries.c.status)
        ).all()
    )
    passed, total = delivery.matching_items(conn, rules, since)
    per_threshold = []
    for t in range(1, 6):
        n, _ = delivery.matching_items(conn, rules.model_copy(update={"min_importance": t}), since)
        per_threshold.append({"min": t, "n": len(n)})
    recent = conn.execute(
        sa.select(
            deliveries.c.item_id,
            deliveries.c.channel,
            deliveries.c.status,
            deliveries.c.sent_at,
            deliveries.c.created_at,
            deliveries.c.last_error,
            SUMMARY.label("summary"),
            subscribers.c.title,
        )
        .join(
            annotations,
            sa.and_(annotations.c.item_id == deliveries.c.item_id, _annotated()),
            isouter=True,
        )
        .join(
            subscribers,
            sa.cast(subscribers.c.chat_id, sa.String) == deliveries.c.channel,
            isouter=True,
        )
        .order_by(deliveries.c.id.desc())
        .limit(15)
    ).all()
    return {
        "counts": counts,
        "preview": {"passed": len(passed), "total": total, "per_threshold": per_threshold},
        "recent": recent,
    }
