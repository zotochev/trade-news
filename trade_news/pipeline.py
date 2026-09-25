"""collect → raw store → dedup → items. One source per call; failures never propagate."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import httpx
import sqlalchemy as sa
import structlog

from trade_news.collectors.base import Batch, CollectorSpec, Context, RawItem
from trade_news.config import Config
from trade_news.db.engine import insert_ignore, insert_ignore_many
from trade_news.db.schema import (
    collector_runs,
    collector_state,
    econ_events,
    items,
    macro_observations,
    rate_expectations,
    raw_items,
)
from trade_news.dedup import content_hash, find_duplicate, normalize_title, normalize_url, sha256
from trade_news.http import RateLimiter, make_getter

log = structlog.get_logger()

# Tables a collector may fill through Batch.rows: existing rows (same key) are left as is;
# a table without key columns is append-only.
ROW_TABLES = {
    "rate_expectations": (
        rate_expectations,
        ("central_bank", "meeting_date", "snapshot_at", "outcome"),
    ),
    "macro_observations": (macro_observations, ("series_id", "obs_date")),
    "econ_events": (econ_events, ()),
}

# SQLite has a single writer anyway, and dedup must see items written by other sources.
WRITE_LOCK = threading.Lock()


@dataclass(slots=True)
class IngestStats:
    fetched: int = 0
    inserted: int = 0  # new raw rows (new items + new revisions)
    seen_before: int = 0  # exact same content already stored or repeated within the batch
    dedup_merged: int = 0  # new items attached to an existing group
    rows: int = 0  # new rows in ROW_TABLES (FedWatch snapshots, FRED observations)


def utcnow() -> datetime:
    return datetime.now(UTC)


def ingest(
    conn: sa.Connection,
    spec: CollectorSpec,
    batch: Batch,
    cfg: Config,
    fetched_at: datetime,
) -> IngestStats:
    unique = _unique(batch.items)
    # e.g. one article returned for several tickers in the same response
    stats = IngestStats(fetched=len(batch.items), seen_before=len(batch.items) - len(unique))
    for it in unique:
        chash = content_hash(it.title, it.body)
        (raw_id,) = insert_ignore(
            conn,
            raw_items,
            [
                {
                    "source": spec.name,
                    "source_item_id": it.source_item_id,
                    "url": it.url,
                    "title": it.title,
                    "body": it.body,
                    "published_at": it.published_at,
                    "fetched_at": fetched_at,
                    "raw_json": _jsonable(it.raw),
                    "content_hash": chash,
                }
            ],
            "source",
            "source_item_id",
            "content_hash",
        )
        if raw_id is None:
            stats.seen_before += 1
            continue
        stats.inserted += 1
        if _upsert_item(conn, spec, it, raw_id, fetched_at, cfg):
            stats.dedup_merged += 1
    for name, rows in batch.rows.items():
        table, conflict_cols = ROW_TABLES[name]
        if not rows:
            continue
        if conflict_cols:
            stats.rows += insert_ignore_many(conn, table, rows, *conflict_cols)
        else:  # snapshot tables: the collector only sends changed rows
            conn.execute(table.insert(), rows)
            stats.rows += len(rows)
    if batch.cursor is not None:
        _save_cursor(conn, spec.name, batch.cursor)
    return stats


def _upsert_item(
    conn: sa.Connection,
    spec: CollectorSpec,
    it: RawItem,
    raw_id: int,
    fetched_at: datetime,
    cfg: Config,
) -> bool:
    """Creates the item (or updates it with a new revision). Returns True if it was merged
    into an existing dedup group."""
    title_norm = normalize_title(it.title)
    values = {
        "raw_item_id": raw_id,
        "canonical_url": normalize_url(it.url),
        "title": it.title,
        "title_norm": title_norm,
        "title_hash": sha256(title_norm)
        if (spec.title_dedup or it.raw.get("dedup_title") == "exact") and title_norm
        else None,
        "body": it.body,
    }
    existing = conn.execute(
        sa.select(items.c.id).where(
            items.c.source == spec.name, items.c.source_item_id == it.source_item_id
        )
    ).scalar()
    if existing is not None:  # new revision of a known item: keep id and group
        conn.execute(items.update().where(items.c.id == existing).values(**values))
        return False

    published_at = it.published_at or fetched_at
    exact_title_only = it.raw.get("dedup_title") == "exact"
    match = find_duplicate(
        conn,
        canonical_url=values["canonical_url"],
        title_norm=title_norm,
        published_at=published_at,
        use_title=spec.title_dedup or exact_title_only,
        cfg=cfg.dedup,
        fuzzy=not exact_title_only,
    )
    item_id = conn.execute(
        items.insert()
        .values(
            **values,
            source=spec.name,
            source_item_id=it.source_item_id,
            published_at=published_at,
            fetched_at=fetched_at,
            dedup_group_id=match.group_id if match else None,
            dedup_reason=match.reason if match else None,
        )
        .returning(items.c.id)
    ).scalar_one()
    if match is None:
        conn.execute(items.update().where(items.c.id == item_id).values(dedup_group_id=item_id))
    return match is not None


def _unique(batch_items: list[RawItem]) -> list[RawItem]:
    """A single response may repeat an item; keep the last occurrence."""
    return list(
        {(it.source_item_id, content_hash(it.title, it.body)): it for it in batch_items}.values()
    )


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _load_cursor(conn: sa.Connection, source: str) -> dict | None:
    return conn.execute(
        sa.select(collector_state.c.cursor).where(collector_state.c.source == source)
    ).scalar()


def _save_cursor(conn: sa.Connection, source: str, cursor: dict) -> None:
    updated = conn.execute(
        collector_state.update()
        .where(collector_state.c.source == source)
        .values(cursor=cursor, updated_at=utcnow())
    )
    if updated.rowcount == 0:
        conn.execute(
            collector_state.insert().values(source=source, cursor=cursor, updated_at=utcnow())
        )


# --- running a source ---------------------------------------------------------


def missing_secrets(spec: CollectorSpec) -> list[str]:
    return [name for name in spec.secrets if not os.environ.get(name)]


def make_context(
    spec: CollectorSpec,
    cfg: Config,
    limiters: dict[str, RateLimiter],
    client: httpx.Client,
    now: Callable[[], datetime] = utcnow,
) -> Context:
    scfg = cfg.sources[spec.name]
    key = scfg.rate_limit or spec.name
    if key not in limiters:
        rl = cfg.rate_limits.get(key)
        limiters[key] = RateLimiter(rl.calls, rl.period) if rl else RateLimiter(1, 1)
    return Context(
        source=spec.name,
        get=make_getter(client, limiters[key], source=spec.name),
        params=scfg.params,
        secrets={name: os.environ[name] for name in spec.secrets},
        now=now,
        log=log.bind(source=spec.name),
    )


def run_source(
    engine: sa.Engine, spec: CollectorSpec, cfg: Config, ctx: Context
) -> IngestStats | None:
    """Runs one collector end-to-end. Never raises: errors are logged and recorded."""
    started = utcnow()
    run_id = None
    try:
        with engine.begin() as conn:
            run_id = conn.execute(
                collector_runs.insert()
                .values(source=spec.name, started_at=started, status="running")
                .returning(collector_runs.c.id)
            ).scalar_one()
        with engine.connect() as conn:
            cursor = _load_cursor(conn, spec.name)
        batch = spec.fetch(ctx, cursor)  # network: outside the write lock
        with WRITE_LOCK, engine.begin() as conn:
            stats = ingest(conn, spec, batch, cfg, fetched_at=utcnow())
            conn.execute(
                collector_runs.update()
                .where(collector_runs.c.id == run_id)
                .values(
                    status="ok",
                    finished_at=utcnow(),
                    fetched=stats.fetched,
                    inserted=stats.inserted,
                    seen_before=stats.seen_before,
                    dedup_merged=stats.dedup_merged,
                )
            )
        log.info("collector_ok", source=spec.name, **asdict(stats), seconds=_secs(started))
        return stats
    except Exception as exc:
        log.exception("collector_error", source=spec.name, error=repr(exc))
        if run_id is None:
            return None
        try:
            with engine.begin() as conn:
                conn.execute(
                    collector_runs.update()
                    .where(collector_runs.c.id == run_id)
                    .values(status="error", finished_at=utcnow(), error=repr(exc)[:2000])
                )
        except Exception:
            log.exception("collector_run_record_failed", source=spec.name)
        return None


def _secs(started: datetime) -> float:
    return round((utcnow() - started).total_seconds(), 2)
