"""Central bank press releases and speeches via their RSS feeds (no key).

Feeds (RSS 2.0, parsing and the old-entry cutoff are in rss.py):
- Fed: https://www.federalreserve.gov/feeds/feeds.htm (press_all.xml, speeches.xml)
- ECB: https://www.ecb.europa.eu/home/html/rss.en.html (press.html: releases, speeches, interviews)
- BoE: https://www.bankofengland.co.uk/rss (news: everything, incl. market notices)
- BoJ: https://www.boj.or.jp/en/rss/ (whatsnew.xml: everything, incl. routine statistics)

One source per bank, so a broken feed doesn't stop the others. Titles are templated
("Minutes of ...", "Foreign Currency Reserves ... Market Notice"), so dedup is by guid/URL only.
Noise (routine operations, statistics) is filtered from the LLM with `llm.exclude` in config.yaml.
"""

from __future__ import annotations

from trade_news.collectors import rss
from trade_news.collectors.base import Batch, Context, RawItem, collector


def _fetch(ctx: Context, bank: str, hint: str, default_feeds: list[str]) -> Batch:
    cutoff = rss.cutoff(ctx)
    items: list[RawItem] = []
    for url in ctx.params.get("feeds", default_feeds):
        entries = rss.fetch_entries(ctx, url)
        items.extend(
            it for e in entries if (it := rss.to_raw_item(e, cutoff, bank=bank, hint=hint))
        )
    return Batch(items=items)


@collector(
    "fed_rss",
    description="ФРС: пресс-релизы и выступления (RSS)",
    title_dedup=False,
)
def fetch_fed(ctx: Context, cursor: dict | None) -> Batch:
    return _fetch(
        ctx,
        "Fed",
        "US Federal Reserve (currency USD)",
        [
            "https://www.federalreserve.gov/feeds/press_all.xml",
            "https://www.federalreserve.gov/feeds/speeches.xml",
        ],
    )


@collector(
    "ecb_rss",
    description="ЕЦБ: пресс-релизы, выступления, интервью (RSS)",
    title_dedup=False,
)
def fetch_ecb(ctx: Context, cursor: dict | None) -> Batch:
    return _fetch(
        ctx,
        "ECB",
        "European Central Bank (currency EUR)",
        ["https://www.ecb.europa.eu/rss/press.html"],
    )


@collector(
    "boe_rss",
    description="Банк Англии: новости и решения (RSS)",
    title_dedup=False,
)
def fetch_boe(ctx: Context, cursor: dict | None) -> Batch:
    return _fetch(
        ctx, "BoE", "Bank of England (currency GBP)", ["https://www.bankofengland.co.uk/rss/news"]
    )


@collector(
    "boj_rss",
    description="Банк Японии: новости и решения (RSS)",
    title_dedup=False,
)
def fetch_boj(ctx: Context, cursor: dict | None) -> Batch:
    return _fetch(
        ctx, "BoJ", "Bank of Japan (currency JPY)", ["https://www.boj.or.jp/en/rss/whatsnew.xml"]
    )
