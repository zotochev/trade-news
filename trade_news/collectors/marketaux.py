"""Marketaux news with ticker entities.

Docs: https://www.marketaux.com/documentation
Free plan (checked 2026-09-25): 100 requests/day, 3 articles per request, 30 requests/min
(headers x-usagelimit-*, x-ratelimit-*). The token is only accepted as the `api_token` query
parameter, so HTTP errors are re-raised without the URL to keep it out of the logs.

With 3 articles per request we can't keep up with the whole US feed, so every run takes the
newest articles (`published_desc`, the default). A daily request budget is counted in the
cursor, so restarts and manual `collect` runs can't exceed the plan.
Response item: {uuid, title, description, snippet, url, published_at, source, entities: [{symbol,
name, type, country, match_score, sentiment_score, ...}], ...}
"""

from __future__ import annotations

from datetime import datetime

import httpx

from trade_news.collectors.base import Batch, Context, RawItem, collector

URL = "https://api.marketaux.com/v1/news/all"
DEFAULT_QUERY = {
    "language": "en",
    "countries": "us",
    "filter_entities": "true",  # return only the entities matching the filters
    "must_have_entities": "true",  # skip articles without a recognised ticker
}


def to_raw_item(a: dict) -> RawItem | None:
    if not a.get("uuid") or not a.get("title"):
        return None
    symbols = [e["symbol"] for e in a.get("entities") or [] if e.get("symbol")]
    raw = dict(a)
    if symbols:
        raw["hint"] = f"tickers: {', '.join(dict.fromkeys(symbols))}"
    return RawItem(
        source_item_id=a["uuid"],
        title=a["title"],
        body=a.get("description") or None,
        url=a.get("url") or None,
        published_at=datetime.fromisoformat(a["published_at"]) if a.get("published_at") else None,
        raw=raw,
    )


@collector(
    "marketaux",
    secrets=("MARKETAUX_API_KEY",),
    description="Marketaux: новости акций США с привязкой к тикерам",
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    cursor = dict(cursor or {})
    today = ctx.now().date().isoformat()
    if cursor.get("day") != today:
        cursor = {"day": today, "calls": 0}
    budget = int(ctx.params.get("daily_request_budget", 80))
    if cursor["calls"] >= budget:
        ctx.log.info("marketaux_budget_spent", calls=cursor["calls"], budget=budget)
        return Batch(items=[], cursor=cursor)

    # Only successful runs save the cursor, so failed calls aren't counted: the budget stays
    # below the plan limit to leave room for them.
    cursor["calls"] += 1
    query = {**DEFAULT_QUERY, **ctx.params.get("query", {})}
    try:
        resp = ctx.get(URL, params={**query, "api_token": ctx.secrets["MARKETAUX_API_KEY"]})
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Marketaux HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        ) from None
    data = resp.json()
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError(f"unexpected Marketaux response: {str(data)[:300]}")
    items = [it for a in data["data"] if (it := to_raw_item(a))]
    return Batch(items=items, cursor=cursor)
