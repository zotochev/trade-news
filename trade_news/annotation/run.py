"""The annotation job: pending items → LLM in batches → validated rows.

- Only dedup group leaders, fetched within llm.max_item_age_hours, not excluded by config.
- Idempotent per (item, PROMPT_VERSION): annotated or dead-lettered items are not picked again.
- Invalid or missing answers get one retry; then the item goes to annotation_dead_letters.
- Quota exhausted / provider down: the run stops, the rest stays pending for the next run.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
import structlog

from trade_news.annotation import assets as asset_ref
from trade_news.annotation.contract import (
    PROMPT_VERSION,
    Annotation,
    ItemAnnotation,
    ItemForAnnotation,
    LLMClient,
    LLMUnavailable,
    QuotaExhausted,
    validate_item,
)
from trade_news.annotation.dates import resolve_relevance
from trade_news.config import LLMConfig
from trade_news.db.engine import insert_ignore
from trade_news.db.schema import (
    annotation_dead_letters,
    annotations,
    asset_resolution_queue,
    item_assets,
    item_relevance,
    items,
    raw_items,
)

log = structlog.get_logger()


@dataclass(slots=True)
class AnnotateStats:
    batches: int = 0
    annotated: int = 0
    retried: int = 0
    dead_lettered: int = 0
    specific_links: int = 0
    unresolved_links: int = 0
    stopped: str | None = None  # quota | unavailable


def pending_items(
    conn: sa.Connection,
    cfg: LLMConfig,
    now: datetime,
    limit: int,
    only_ids: list[int] | None = None,
) -> list[ItemForAnnotation]:
    """Items waiting for annotation. `only_ids` (manual re-annotation from the admin page)
    skips the age and exclusion filters."""
    done = sa.select(annotations.c.item_id).where(annotations.c.prompt_version == PROMPT_VERSION)
    dead = sa.select(annotation_dead_letters.c.item_id).where(
        annotation_dead_letters.c.prompt_version == PROMPT_VERSION
    )
    whole_source_excluded = [] if only_ids else [e.source for e in cfg.exclude if not e.title_regex]
    q = (
        sa.select(
            items.c.id,
            items.c.source,
            items.c.published_at,
            items.c.title,
            items.c.body,
            raw_items.c.raw_json,
        )
        .join(raw_items, raw_items.c.id == items.c.raw_item_id)
        .where(
            items.c.id.not_in(done),
            items.c.id.not_in(dead),
            items.c.source.not_in(whole_source_excluded),
        )
        .order_by(items.c.published_at.desc())
    )
    if only_ids:
        q = q.where(items.c.id.in_(only_ids))
    else:
        q = q.where(
            items.c.id == items.c.dedup_group_id,
            items.c.fetched_at >= now - timedelta(hours=cfg.max_item_age_hours),
        )
    regexes = (
        []
        if only_ids
        else [(e.source, re.compile(e.title_regex)) for e in cfg.exclude if e.title_regex]
    )
    out = []
    for row in conn.execute(q):
        if any(src == row.source and rx.search(row.title or "") for src, rx in regexes):
            continue
        if not only_ids and (row.raw_json or {}).get("llm_skip"):
            continue  # the source already decided it's noise (e.g. a small Form 4)
        out.append(
            ItemForAnnotation(
                id=row.id,
                source=row.source,
                published_at=row.published_at,
                title=row.title or "",
                body=row.body or "",
                hints=_hints(conn, row.source, row.raw_json or {}),
            )
        )
        if len(out) >= limit:
            break
    return out


def _hints(conn: sa.Connection, source: str, raw: dict) -> str | None:
    """Facts the source already knows, so the LLM doesn't have to guess the instrument."""
    if source == "sec_edgar" and raw.get("cik"):
        ticker = (raw.get("form4") or {}).get("ticker") or asset_ref.equity_by_cik(conn, raw["cik"])
        form = raw.get("form")
        return f"SEC form {form}; issuer ticker: {ticker or 'unknown'}"
    if raw.get("hint"):  # set by the collector, e.g. "Bank of England (currency GBP)"
        return raw["hint"]
    if raw.get("related"):
        return f"related tickers: {raw['related']}"
    return None


def annotate_pending(
    engine: sa.Engine,
    client: LLMClient,
    cfg: LLMConfig,
    now: datetime,
    only_ids: list[int] | None = None,
) -> AnnotateStats:
    stats = AnnotateStats()
    with engine.connect() as conn:
        limit = len(only_ids) if only_ids else cfg.batch_size * cfg.max_batches_per_run
        todo = pending_items(conn, cfg, now, limit, only_ids)
    for i in range(0, len(todo), cfg.batch_size):
        batch = todo[i : i + cfg.batch_size]
        try:
            _annotate_batch(engine, client, batch, stats, now)
        except QuotaExhausted:
            stats.stopped = "quota"
            log.warning("llm_quota_exhausted", pending=len(todo) - i)
            break
        except LLMUnavailable as exc:
            stats.stopped = "unavailable"
            log.error("llm_unavailable", error=str(exc), pending=len(todo) - i)
            break
    if stats.batches or stats.stopped:
        log.info("annotate_done", **asdict(stats), pending_seen=len(todo))
    return stats


def _annotate_batch(engine, client: LLMClient, batch: list[ItemForAnnotation], stats, now) -> None:
    stats.batches += 1
    by_id = {it.id: it for it in batch}
    failed = _store_valid(engine, by_id, client.annotate_batch(batch), stats, now)
    if not failed:
        return
    stats.retried += len(failed)
    retry = [by_id[i] for i in failed]
    still = _store_valid(engine, by_id, client.annotate_batch(retry), stats, now, previous=failed)
    with engine.begin() as conn:
        for item_id, (error, payload) in still.items():
            insert_ignore(
                conn,
                annotation_dead_letters,
                [
                    {
                        "item_id": item_id,
                        "prompt_version": PROMPT_VERSION,
                        "created_at": now,
                        "error": error,
                        "payload_json": payload,
                    }
                ],
                "item_id",
                "prompt_version",
            )
    stats.dead_lettered += len(still)
    if still:
        log.warning("annotation_dead_lettered", item_ids=sorted(still))


def _store_valid(engine, by_id, results: list[Annotation], stats, now, previous=None) -> dict:
    """Stores valid annotations. Returns {item_id: (error, payload)} for the rest of the batch."""
    expected = set(previous) if previous is not None else set(by_id)
    failed: dict[int, tuple[str, dict | None]] = {
        i: ("missing in LLM response", None) for i in expected
    }
    for ann in results:
        if ann.item_id not in expected:
            continue
        parsed = validate_item(ann.payload)
        if isinstance(parsed, str):
            failed[ann.item_id] = (parsed, ann.payload)
            continue
        with engine.begin() as conn:
            _persist(conn, by_id[ann.item_id], ann, parsed, stats, now)
        failed.pop(ann.item_id, None)  # the model may repeat an id; storing is idempotent
    return failed


def _persist(
    conn, item: ItemForAnnotation, ann: Annotation, parsed: ItemAnnotation, stats, now
) -> None:
    (annotation_id,) = insert_ignore(
        conn,
        annotations,
        [
            {
                "item_id": item.id,
                "model": ann.model,
                "prompt_version": PROMPT_VERSION,
                "created_at": now,
                "payload_json": ann.payload,
                "input_tokens": ann.input_tokens,
                "output_tokens": ann.output_tokens,
                "cost_estimate": ann.cost_estimate,
            }
        ],
        "item_id",
        "prompt_version",
    )
    if annotation_id is None:  # already annotated with this prompt version: idempotent no-op
        return
    stats.annotated += 1
    for link in parsed.assets:
        asset_id = None
        if link.scope == "specific":
            stats.specific_links += 1
            asset_id = asset_ref.resolve(conn, link.asset_class, link.symbol_or_name)
            if asset_id is None:
                stats.unresolved_links += 1
                conn.execute(
                    asset_resolution_queue.insert().values(
                        item_id=item.id,
                        annotation_id=annotation_id,
                        asset_class=link.asset_class,
                        symbol_or_name=link.symbol_or_name,
                        created_at=now,
                    )
                )
        conn.execute(
            item_assets.insert().values(
                item_id=item.id,
                annotation_id=annotation_id,
                asset_class=link.asset_class,
                scope=link.scope,
                asset_id=asset_id,
                raw_symbol=link.symbol_or_name,
                group_label=link.group_label if link.scope == "group" else None,
                direction=link.direction,
                importance=link.importance,
                confidence=round(link.confidence, 2),
                is_primary=link.is_primary,
            )
        )
    conn.execute(
        item_relevance.insert().values(
            item_id=item.id,
            annotation_id=annotation_id,
            **resolve_relevance(parsed.relevance, anchor=item.published_at),
        )
    )
