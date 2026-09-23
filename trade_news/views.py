"""Read helpers for an annotated item, shared by the admin page and Telegram delivery."""

from __future__ import annotations

from collections import defaultdict

import sqlalchemy as sa

from trade_news.annotation.contract import PROMPT_VERSION
from trade_news.db.schema import annotations, assets, item_assets, item_relevance, items, raw_items

SUMMARY = annotations.c.payload_json["summary"].as_string()
EVENT_TYPE = annotations.c.payload_json["event_type"].as_string()


def current_annotation():
    return annotations.c.prompt_version == PROMPT_VERSION


def links_by_item(conn, item_ids: list[int]) -> dict[int, list[dict]]:
    if not item_ids:
        return {}
    rows = conn.execute(
        sa.select(item_assets, assets.c.symbol)
        .outerjoin(assets, assets.c.id == item_assets.c.asset_id)
        .where(item_assets.c.item_id.in_(item_ids))
        .order_by(item_assets.c.is_primary.desc(), item_assets.c.importance.desc())
    ).mappings()
    out: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        label = r["symbol"] or (
            f"?{r['raw_symbol']}" if r["raw_symbol"] else (r["group_label"] or r["asset_class"])
        )
        out[r["item_id"]].append(
            dict(r) | {"label": label, "unresolved": bool(r["raw_symbol"]) and not r["symbol"]}
        )
    return out


def relevance_by_item(conn, item_ids: list[int]) -> dict[int, dict]:
    if not item_ids:
        return {}
    rows = conn.execute(sa.select(item_relevance).where(item_relevance.c.item_id.in_(item_ids)))
    return {r.item_id: dict(r._mapping) for r in rows}


def news_item(conn, item_id: int) -> dict | None:
    row = conn.execute(
        sa.select(
            items,
            annotations.c.payload_json,
            annotations.c.model,
            annotations.c.input_tokens,
            annotations.c.output_tokens,
            annotations.c.cost_estimate,
            annotations.c.prompt_version,
            raw_items.c.url.label("raw_url"),
        )
        .outerjoin(annotations, sa.and_(annotations.c.item_id == items.c.id, current_annotation()))
        .join(raw_items, raw_items.c.id == items.c.raw_item_id)
        .where(items.c.id == item_id)
    ).first()
    if row is None:
        return None
    out = dict(row._mapping)
    out["links"] = links_by_item(conn, [item_id]).get(item_id, [])
    out["relevance"] = relevance_by_item(conn, [item_id]).get(item_id)
    out["duplicates"] = [
        dict(r._mapping)
        for r in conn.execute(
            sa.select(items.c.id, items.c.source, items.c.title, items.c.dedup_reason).where(
                items.c.dedup_group_id == row.dedup_group_id, items.c.id != item_id
            )
        )
    ]
    return out
