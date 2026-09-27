"""Read helpers for an annotated item, shared by the admin page and Telegram delivery."""

from __future__ import annotations

from collections import defaultdict

import sqlalchemy as sa

from trade_news import sectors
from trade_news.db.schema import (
    annotations,
    assets,
    item_assets,
    item_relevance,
    item_sectors,
    items,
    raw_items,
)

SUMMARY = annotations.c.payload_json["summary"].as_string()
EVENT_TYPE = annotations.c.payload_json["event_type"].as_string()


def latest_annotation_ids():
    """Newest annotation per item. After a prompt version bump an item can have several;
    readers must use one of them, never mix their links."""
    return sa.select(sa.func.max(annotations.c.id)).group_by(annotations.c.item_id)


def current_annotation():
    return annotations.c.id.in_(latest_annotation_ids())


def links_by_item(conn, item_ids: list[int]) -> dict[int, list[dict]]:
    if not item_ids:
        return {}
    rows = conn.execute(
        sa.select(item_assets, assets.c.symbol)
        .outerjoin(assets, assets.c.id == item_assets.c.asset_id)
        .where(
            item_assets.c.item_id.in_(item_ids),
            item_assets.c.annotation_id.in_(latest_annotation_ids()),
        )
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
    rows = conn.execute(
        sa.select(item_relevance).where(
            item_relevance.c.item_id.in_(item_ids),
            item_relevance.c.annotation_id.in_(latest_annotation_ids()),
        )
    )
    return {r.item_id: dict(r._mapping) for r in rows}


# --- sectors ----------------------------------------------------------------------------
# An item's sectors = what the LLM named (item_sectors) ∪ sectors of its main assets (assets
# filled later still count: computed at read time) ∪ FX labels of its currency pairs.
# A passing mention doesn't count: only primary links or links with importance >= 4.

SectorPair = tuple[str, str | None]  # (sector, industry or None for the whole sector)


def _main_link():
    return sa.and_(
        item_assets.c.annotation_id.in_(latest_annotation_ids()),
        sa.or_(item_assets.c.is_primary.is_(True), item_assets.c.importance >= 4),
    )


def sectors_by_item(conn, item_ids: list[int]) -> dict[int, list[SectorPair]]:
    """Ordered, without duplicates; a bare sector is dropped when one of its industries is
    there (('technology', None) + ('technology', 'software') → only the latter)."""
    if not item_ids:
        return {}
    found: dict[int, list[SectorPair]] = defaultdict(list)
    for r in conn.execute(
        sa.select(item_sectors.c.item_id, item_sectors.c.sector, item_sectors.c.industry)
        .where(
            item_sectors.c.item_id.in_(item_ids),
            item_sectors.c.annotation_id.in_(latest_annotation_ids()),
        )
        .order_by(item_sectors.c.id)
    ):
        found[r.item_id].append((r.sector, r.industry))
    for r in conn.execute(
        sa.select(
            item_assets.c.item_id, assets.c.asset_class, assets.c.sector, assets.c.industry,
            assets.c.base_ccy, assets.c.quote_ccy,
        )
        .join(assets, assets.c.id == item_assets.c.asset_id)
        .where(item_assets.c.item_id.in_(item_ids), _main_link())
        .order_by(item_assets.c.is_primary.desc(), item_assets.c.importance.desc())
    ):  # fmt: skip
        if r.asset_class == "fx":
            found[r.item_id] += [("fx", k) for k in sectors.fx_industries(r.base_ccy, r.quote_ccy)]
        elif r.sector:
            found[r.item_id].append((r.sector, r.industry))
    out = {}
    for item_id, pairs in found.items():
        unique = list(dict.fromkeys(pairs))
        with_industry = {s for s, i in unique if i}
        out[item_id] = [(s, i) for s, i in unique if i or s not in with_industry]
    return out


def sector_filter(key: str):
    """WHERE clause on items.c.id: the item is in this sector or industry (same union as
    sectors_by_item). Unknown key → matches nothing."""
    expanded = sectors.expand(key)
    if expanded is None:
        return sa.false()
    sector, industry = expanded
    llm_match = item_sectors.c.sector == sector
    if industry:
        llm_match = item_sectors.c.industry == industry
    by_llm = sa.select(item_sectors.c.item_id).where(
        item_sectors.c.annotation_id.in_(latest_annotation_ids()), llm_match
    )
    if industry in ("fx_commodity", "fx_safe_haven", "fx_majors", "fx_emerging"):
        asset_match = _fx_label_match(industry)
    elif industry:
        asset_match = assets.c.industry == industry
    else:
        asset_match = assets.c.sector == sector
    by_asset = (
        sa.select(item_assets.c.item_id)
        .join(assets, assets.c.id == item_assets.c.asset_id)
        .where(_main_link(), asset_match)
    )
    return sa.or_(items.c.id.in_(by_llm), items.c.id.in_(by_asset))


def _fx_label_match(label: str):
    """Same rules as sectors.fx_industries, in SQL on the pair's currencies."""
    base, quote = assets.c.base_ccy, assets.c.quote_ccy
    either = {
        "fx_commodity": sectors.COMMODITY_CCY,
        "fx_safe_haven": sectors.SAFE_HAVEN_CCY,
    }.get(label)
    if either is not None:
        cond = sa.or_(base.in_(sorted(either)), quote.in_(sorted(either)))
    else:
        both_g10 = sa.and_(base.in_(sorted(sectors.G10)), quote.in_(sorted(sectors.G10)))
        cond = both_g10 if label == "fx_majors" else sa.not_(both_g10)
    return sa.and_(assets.c.asset_class == "fx", cond)


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
            raw_items.c.raw_json,
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
    out["sectors"] = sectors_by_item(conn, [item_id]).get(item_id, [])
    out["duplicates"] = [
        dict(r._mapping)
        for r in conn.execute(
            sa.select(items.c.id, items.c.source, items.c.title, items.c.dedup_reason).where(
                items.c.dedup_group_id == row.dedup_group_id, items.c.id != item_id
            )
        )
    ]
    return out
