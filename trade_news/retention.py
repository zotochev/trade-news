"""Deleting old data so the database doesn't grow without bound.

Whole dedup groups are removed together with everything hanging off them:
- groups whose leader was never annotated: after retention.unannotated_days (the bulk: Form 4,
  first-run backlog, items that never reached the LLM);
- annotated groups: after retention.annotated_days (kept longer: validation needs history).
Raw rows no longer referenced by an item, and service logs (collector_runs, llm_calls) older
than retention.logs_days are removed too. SQLite reuses freed pages, so the file stops growing.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
import structlog

from trade_news.config import RetentionConfig
from trade_news.db.schema import (
    annotation_dead_letters,
    annotations,
    asset_resolution_queue,
    collector_runs,
    deliveries,
    item_assets,
    item_relevance,
    items,
    llm_calls,
    raw_items,
)

log = structlog.get_logger()

CHUNK = 500
_ITEM_CHILDREN = (
    item_assets,
    item_relevance,
    asset_resolution_queue,
    annotation_dead_letters,
    deliveries,
    annotations,  # after the tables that reference annotations.id
)


@dataclass(slots=True)
class CleanupStats:
    groups: int = 0
    items: int = 0
    raw_items: int = 0
    log_rows: int = 0


def doomed_leaders(conn: sa.Connection, cfg: RetentionConfig, now: datetime) -> list[int]:
    annotated = sa.select(annotations.c.item_id)
    old_unannotated = sa.and_(
        items.c.fetched_at < now - timedelta(days=cfg.unannotated_days),
        items.c.id.not_in(annotated),
    )
    old_any = items.c.fetched_at < now - timedelta(days=cfg.annotated_days)
    q = sa.select(items.c.id).where(
        items.c.id == items.c.dedup_group_id, sa.or_(old_unannotated, old_any)
    )
    return list(conn.execute(q).scalars())


def cleanup(
    engine: sa.Engine, cfg: RetentionConfig, now: datetime, write_lock=None
) -> CleanupStats:
    stats = CleanupStats()
    with engine.connect() as conn:
        leaders = doomed_leaders(conn, cfg, now)
    for i in range(0, len(leaders), CHUNK):
        chunk = leaders[i : i + CHUNK]
        with write_lock or nullcontext(), engine.begin() as conn:
            ids = list(
                conn.execute(
                    sa.select(items.c.id).where(items.c.dedup_group_id.in_(chunk))
                ).scalars()
            )
            for table in _ITEM_CHILDREN:
                conn.execute(table.delete().where(table.c.item_id.in_(ids)))
            stats.items += conn.execute(items.delete().where(items.c.id.in_(ids))).rowcount
            stats.groups += len(chunk)

    raw_cutoff = now - timedelta(days=min(cfg.unannotated_days, cfg.annotated_days))
    logs_cutoff = now - timedelta(days=cfg.logs_days)
    with write_lock or nullcontext(), engine.begin() as conn:
        stats.raw_items = conn.execute(
            raw_items.delete().where(
                raw_items.c.fetched_at < raw_cutoff,
                raw_items.c.id.not_in(sa.select(items.c.raw_item_id)),
            )
        ).rowcount
        stats.log_rows += conn.execute(
            collector_runs.delete().where(collector_runs.c.started_at < logs_cutoff)
        ).rowcount
        stats.log_rows += conn.execute(
            llm_calls.delete().where(llm_calls.c.started_at < logs_cutoff)
        ).rowcount
    log.info("cleanup_done", **asdict(stats))
    return stats
