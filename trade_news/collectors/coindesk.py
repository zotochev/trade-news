"""CoinDesk crypto news via RSS (no key).

Feed: https://www.coindesk.com/arc/outboundfeeds/rss (RSS 2.0, ~25 newest articles; the URL with
a trailing slash answers 308). Parsing and the old-entry cutoff are in rss.py.
Each item carries a section (Markets, Business, Policy, ...) and topic tags as <category>;
they go to the LLM as a hint.
"""

from __future__ import annotations

from trade_news.collectors import rss
from trade_news.collectors.base import Batch, Context, collector

FEED_URL = "https://www.coindesk.com/arc/outboundfeeds/rss"


@collector("coindesk", description="CoinDesk: новости крипторынка (RSS)")
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    cutoff = rss.cutoff(ctx)
    items = []
    for e in rss.fetch_entries(ctx, ctx.params.get("feed", FEED_URL)):
        topics = [c for c in e["categories"] if c != "News"]
        hint = "crypto news" + (f"; topics: {', '.join(topics)}" if topics else "")
        if it := rss.to_raw_item(e, cutoff, hint=hint):
            items.append(it)
    return Batch(items=items)
