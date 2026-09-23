"""Finnhub news.

Docs: https://finnhub.io/docs/api/market-news, https://finnhub.io/docs/api/company-news
Free plan: 60 calls/min. /company-news: North American companies only, 1 year of history.
Response item: {category, datetime (unix s), headline, id, image, related, source, summary, url}
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trade_news.collectors.base import Batch, Context, RawItem, collector

BASE_URL = "https://finnhub.io/api/v1"


def to_raw_item(n: dict) -> RawItem:
    return RawItem(
        source_item_id=str(n["id"]),
        title=n.get("headline") or None,
        body=n.get("summary") or None,
        url=n.get("url") or None,
        published_at=datetime.fromtimestamp(n["datetime"], UTC) if n.get("datetime") else None,
        raw=n,
    )


def _get(ctx: Context, path: str, **params) -> list[dict]:
    resp = ctx.get(
        f"{BASE_URL}{path}",
        params=params,
        headers={"X-Finnhub-Token": ctx.secrets["FINNHUB_API_KEY"]},
    )
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"unexpected Finnhub response for {path}: {str(data)[:200]}")
    return data


@collector(
    "finnhub_market_news",
    secrets=("FINNHUB_API_KEY",),
    description="Finnhub: общие рыночные новости и M&A",
)
def fetch_market_news(ctx: Context, cursor: dict | None) -> Batch:
    """General market news; `minId` makes each call incremental per category."""
    cursor = dict(cursor or {})
    items: list[RawItem] = []
    for category in ctx.params.get("categories", ["general"]):
        min_id = int(cursor.get(category, 0))
        news = _get(ctx, "/news", category=category, minId=min_id)
        items.extend(to_raw_item(n) for n in news if n.get("id"))
        if news:
            cursor[category] = max([min_id, *(int(n["id"]) for n in news if n.get("id"))])
    return Batch(items=items, cursor=cursor)


@collector(
    "finnhub_company_news",
    secrets=("FINNHUB_API_KEY",),
    description="Finnhub: новости по тикерам из списка наблюдения",
)
def fetch_company_news(ctx: Context, cursor: dict | None) -> Batch:
    """News per ticker from the watchlist. No server-side cursor: re-reads the last N days,
    already-stored ids are skipped by the raw store's unique key."""
    today = ctx.now().date()
    start = today - timedelta(days=int(ctx.params.get("lookback_days", 2)))
    items: list[RawItem] = []
    for symbol in ctx.params.get("symbols", []):
        news = _get(
            ctx,
            "/company-news",
            symbol=symbol,
            **{"from": start.isoformat(), "to": today.isoformat()},
        )
        items.extend(to_raw_item(n) for n in news if n.get("id"))
    return Batch(items=items, cursor=None)
