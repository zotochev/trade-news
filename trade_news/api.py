"""Read-only HTTP API over the annotated news (mounted at /api inside the admin app).

What goes out: our annotation (Russian summary, linked assets with direction and importance,
relevance dates, sectors, the whale block), the source's title and a link to the original.
The feed text itself is not served: the free news APIs we collect from don't allow
redistribution. Access control is nginx basic auth with its own user file (deploy/HTTPS.md).

Pagination is by an opaque cursor (published_at + id of the last row), newest first, so a
client can walk a whole week without gaps or repeats while new items keep arriving.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from trade_news import sectors as sector_ref
from trade_news import views
from trade_news.annotation.whale import whale_of
from trade_news.db.schema import annotations, assets, item_assets, items, raw_items

API_VERSION = "1.0"
MAX_LIMIT = 500

AssetClass = Literal["equity", "fx", "crypto", "commodity", "index", "rates", "macro"]
Direction = Literal["bullish", "bearish", "neutral"]
EventType = Literal[
    "earnings", "guidance", "m_and_a", "regulatory", "macro", "rate_decision", "cb_speech",
    "insider", "other",
]  # fmt: skip


# --- response models (they are the OpenAPI schema) -------------------------------------


class AssetLink(BaseModel):
    symbol: str | None = Field(description="Our asset id: ticker, pair, coin; null if unresolved")
    label: str = Field(description="Symbol, or the group / the LLM's name when unresolved")
    asset_class: AssetClass
    scope: Literal["market_wide", "group", "specific"]
    direction: Direction | None = Field(description="Expected effect on THIS asset")
    importance: int = Field(ge=1, le=5, description="1 noise … 5 market-moving, for THIS asset")
    confidence: float | None
    is_primary: bool | None = Field(description="The main subject of the news")


class Relevance(BaseModel):
    type: Literal["immediate", "scheduled", "window", "unknown"]
    start: datetime | None = Field(description="UTC; for scheduled events and windows")
    end: datetime | None = Field(description="UTC; end of a window")
    precision: str | None = Field(description="exact | day | month | quarter | unknown")
    raw_phrase: str | None = Field(description="The date expression as in the text")


class Sector(BaseModel):
    sector: str = Field(description="Sector key, see /api/sectors")
    industry: str | None = Field(description="Industry key; null: the sector as a whole")
    name: str = Field(description="Russian name of the industry (or the sector)")


class Whale(BaseModel):
    """A big buyer taking or fighting for a stake. Strength is computed by rules, not by the
    LLM: strong = revision / competing bid, stake within 5 pp of the takeover threshold, or
    repeated insider buying; weak = rumor, denial, or neither price nor stake."""

    kind: Literal["stake", "bid", "bid_revision", "activist", "insider"]
    strength: Literal["strong", "normal", "weak"]
    buyer: str | None
    buyer_type: str | None
    target_ticker: str | None
    exchange: str | None
    listing_country: str | None
    stake_before_pct: float | None
    stake_after_pct: float | None
    takeover_threshold_pct: float | None
    price_per_share: float | None
    currency: str | None
    amount_usd: float | None = Field(None, description="Insider purchases: total in USD")
    cluster_count: int | None = Field(None, description="Insider: purchase days in 30 days")
    conditions: str | None
    rumor: bool | None
    denied: bool | None


class NewsItem(BaseModel):
    id: int
    published_at: datetime = Field(description="UTC")
    fetched_at: datetime = Field(description="UTC, when we collected it")
    source: str = Field(description="Collector name, e.g. sec_edgar, finnhub_market_news")
    title: str
    summary: str | None = Field(description="One line in Russian, written by the LLM")
    event_type: EventType | None
    url: str | None = Field(description="Link to the original")
    main_asset: AssetLink | None = Field(description="The primary link with highest importance")
    assets: list[AssetLink]
    relevance: Relevance | None
    sectors: list[Sector]
    whale: Whale | None


class NewsPage(BaseModel):
    items: list[NewsItem]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null: end")


class IndustryRef(BaseModel):
    key: str
    name: str


class SectorRef(BaseModel):
    key: str
    name: str
    cycle: str | None = Field(description="cyclical | sensitive | defensive (Morningstar)")
    industries: list[IndustryRef]


# --- queries ----------------------------------------------------------------------------


def _encode(published_at: datetime, item_id: int) -> str:
    raw = f"{published_at.isoformat()}|{item_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        when, item_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(when), int(item_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "bad cursor") from exc


def _main(links: list[AssetLink]) -> AssetLink | None:
    primary = [x for x in links if x.is_primary] or links
    return max(primary, key=lambda x: x.importance) if primary else None


def _to_items(conn, rows) -> list[NewsItem]:
    ids = [r.id for r in rows]
    links = views.links_by_item(conn, ids)
    rel = views.relevance_by_item(conn, ids)
    secs = views.sectors_by_item(conn, ids)
    out = []
    for r in rows:
        item_links = [
            AssetLink(
                symbol=x["symbol"], label=x["label"], asset_class=x["asset_class"],
                scope=x["scope"], direction=x["direction"], importance=x["importance"],
                confidence=x["confidence"], is_primary=x["is_primary"],
            )
            for x in links.get(r.id, [])
        ]  # fmt: skip
        rv = rel.get(r.id)
        w = whale_of({"payload_json": r.payload_json})
        out.append(
            NewsItem(
                id=r.id,
                published_at=r.published_at,
                fetched_at=r.fetched_at,
                source=r.source,
                title=r.title or "",
                summary=(r.payload_json or {}).get("summary"),
                event_type=(r.payload_json or {}).get("event_type"),
                url=r.raw_url or r.canonical_url,
                main_asset=_main(item_links),
                assets=item_links,
                relevance=Relevance(
                    type=rv["relevance_type"], start=rv["relevant_from"], end=rv["relevant_to"],
                    precision=rv["date_precision"], raw_phrase=rv["raw_phrase"],
                ) if rv else None,
                sectors=[
                    Sector(sector=s, industry=i, name=sector_ref.name_ru(i or s))
                    for s, i in secs.get(r.id, [])
                ],
                whale=Whale(kind=w["whale_kind"], **w) if w else None,
            )
        )  # fmt: skip
    return out


def _base_query():
    return (
        sa.select(
            items.c.id,
            items.c.published_at,
            items.c.fetched_at,
            items.c.source,
            items.c.title,
            items.c.canonical_url,
            raw_items.c.url.label("raw_url"),
            annotations.c.payload_json,
        )
        .join(annotations, sa.and_(annotations.c.item_id == items.c.id, views.current_annotation()))
        .join(raw_items, raw_items.c.id == items.c.raw_item_id)
    )


# --- the app ----------------------------------------------------------------------------

DESCRIPTION = """\
Annotated market news from trade-news: SEC EDGAR filings (8-K, 10-Q/10-K, Form 4, Schedule
13D), Finnhub, Marketaux, CoinDesk, central banks, FRED, CME FedWatch and the economic
calendar. Each item carries the LLM annotation: a one-line Russian summary, the assets it is
about with direction and importance (per asset), when it applies, sectors, and the
"whale in the capital" block for big stake buyers.

Only one item per group of duplicates is served (the group's leader), annotated items only.
Times are UTC. Pagination: newest first; follow `next_cursor` until it is null.
"""


def create_api(engine: sa.Engine) -> FastAPI:
    api = FastAPI(
        title="trade-news API",
        version=API_VERSION,
        description=DESCRIPTION,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    @api.get("/news", response_model=NewsPage, summary="Annotated news, newest first")
    def list_news(
        since: Annotated[datetime | None, Query(description="published_at >= since (UTC)")] = None,
        until: Annotated[datetime | None, Query(description="published_at < until (UTC)")] = None,
        sector: Annotated[
            str | None, Query(description="Sector or industry key (see /api/sectors)")
        ] = None,
        asset: Annotated[str | None, Query(description="Linked asset symbol: AAPL, EURUSD")] = None,
        asset_class: AssetClass | None = None,
        min_importance: Annotated[
            int | None, Query(ge=1, le=5, description="Some linked asset has importance >= this")
        ] = None,
        direction: Annotated[
            Direction | None, Query(description="Some linked asset has this direction")
        ] = None,
        event_type: EventType | None = None,
        source: Annotated[str | None, Query(description="Collector name")] = None,
        whale: Annotated[
            bool | None, Query(description="true: only whales; false: no whales")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
        cursor: Annotated[str | None, Query(description="next_cursor of the previous page")] = None,
    ) -> NewsPage:
        q = _base_query()
        if since:
            q = q.where(items.c.published_at >= since)
        if until:
            q = q.where(items.c.published_at < until)
        if sector:
            if sector_ref.expand(sector) is None:
                raise HTTPException(400, f"unknown sector key: {sector}")
            q = q.where(views.sector_filter(sector))
        link_cond = [item_assets.c.item_id == items.c.id,
                     item_assets.c.annotation_id == annotations.c.id]  # fmt: skip
        if asset_class:
            link_cond.append(item_assets.c.asset_class == asset_class)
        if min_importance:
            link_cond.append(item_assets.c.importance >= min_importance)
        if direction:
            link_cond.append(item_assets.c.direction == direction)
        if asset:
            link_cond.append(
                item_assets.c.asset_id.in_(
                    sa.select(assets.c.id).where(sa.func.upper(assets.c.symbol) == asset.upper())
                )
            )
        if len(link_cond) > 2:
            q = q.where(sa.exists().where(*link_cond))
        if event_type:
            q = q.where(views.EVENT_TYPE == event_type)  # noqa: SIM300 (a column, not a constant)
        if source:
            q = q.where(items.c.source == source)
        if whale is not None:
            flag = annotations.c.payload_json[("whale", "whale")].as_boolean().is_(True)
            q = q.where(flag if whale else sa.not_(sa.func.coalesce(flag, False)))
        if cursor:
            at, last_id = _decode(cursor)
            q = q.where(
                sa.or_(
                    items.c.published_at < at,
                    sa.and_(items.c.published_at == at, items.c.id < last_id),
                )
            )
        q = q.order_by(items.c.published_at.desc(), items.c.id.desc()).limit(limit + 1)
        with engine.connect() as conn:
            rows = conn.execute(q).all()
            more = len(rows) > limit
            rows = rows[:limit]
            page = _to_items(conn, rows)
        last = rows[-1] if rows else None
        return NewsPage(
            items=page, next_cursor=_encode(last.published_at, last.id) if more and last else None
        )

    @api.get("/news/{item_id}", response_model=NewsItem, summary="One annotated news item")
    def get_news(item_id: int) -> NewsItem:
        with engine.connect() as conn:
            rows = conn.execute(_base_query().where(items.c.id == item_id)).all()
            if not rows:
                raise HTTPException(404, "not found or not annotated")
            return _to_items(conn, rows)[0]

    @api.get("/sectors", response_model=list[SectorRef], summary="Sector / industry keys")
    def list_sectors() -> list[SectorRef]:
        return [
            SectorRef(
                key=s.key,
                name=s.name_ru,
                cycle=s.cycle,
                industries=[IndustryRef(key=i.key, name=i.name_ru) for i in s.industries],
            )
            for s in sector_ref.SECTORS
        ]

    return api
