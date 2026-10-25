"""Crypto news outlets via RSS (no keys). Parsing and the old-entry cutoff are in rss.py.

One source per outlet, so a broken feed doesn't stop the others:
- CoinDesk: https://www.coindesk.com/arc/outboundfeeds/rss (~25 newest). Since 2026-10-23 its
  Vercel firewall answers 403 (`x-vercel-mitigated: deny`) to our server, so it is disabled in
  config.yaml; kept for when access comes back.
- Cointelegraph: https://cointelegraph.com/rss (~30 newest).
- Decrypt: https://decrypt.co/feed (~40 newest; also AI and gaming, dropped by `skip_categories`).
- The Block: https://www.theblock.co/rss.xml (~20 newest).
Sections and topic tags (<category>) go to the LLM as a hint. The same story from several
outlets is merged by the usual title dedup.
"""

from __future__ import annotations

from trade_news.collectors import rss
from trade_news.collectors.base import Batch, Context, collector

FEED_URL = "https://www.coindesk.com/arc/outboundfeeds/rss"
GENERIC = {"News", "Latest News"}  # categories that say nothing


def _fetch(ctx: Context, default_feed: str) -> Batch:
    cutoff = rss.cutoff(ctx)
    skip = set(ctx.params.get("skip_categories", []))
    items = []
    for e in rss.fetch_entries(ctx, ctx.params.get("feed", default_feed)):
        if skip & set(e["categories"]):
            continue
        topics = [c for c in e["categories"] if c not in GENERIC]
        hint = "crypto news" + (f"; topics: {', '.join(topics)}" if topics else "")
        if it := rss.to_raw_item(e, cutoff, hint=hint):
            items.append(it)
    return Batch(items=items)


@collector("coindesk", description="CoinDesk: новости крипторынка (RSS)")
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    return _fetch(ctx, FEED_URL)


@collector("cointelegraph", description="Cointelegraph: новости крипторынка (RSS)")
def fetch_cointelegraph(ctx: Context, cursor: dict | None) -> Batch:
    return _fetch(ctx, "https://cointelegraph.com/rss")


@collector("decrypt", description="Decrypt: новости крипторынка (RSS)")
def fetch_decrypt(ctx: Context, cursor: dict | None) -> Batch:
    return _fetch(ctx, "https://decrypt.co/feed")


@collector("theblock", description="The Block: новости крипторынка (RSS)")
def fetch_theblock(ctx: Context, cursor: dict | None) -> Batch:
    return _fetch(ctx, "https://www.theblock.co/rss.xml")
