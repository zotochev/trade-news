"""Central bank press releases and speeches via their RSS feeds (no key).

Feeds (RSS 2.0, newest first, no paging or `since` parameter, so every run re-reads the
whole feed and already-stored guids are skipped by the raw store's unique key):
- Fed: https://www.federalreserve.gov/feeds/feeds.htm (press_all.xml, speeches.xml)
- ECB: https://www.ecb.europa.eu/home/html/rss.en.html (press.html: releases, speeches, interviews)
- BoE: https://www.bankofengland.co.uk/rss (news: everything, incl. market notices)
- BoJ: https://www.boj.or.jp/en/rss/ (whatsnew.xml: everything, incl. routine statistics)

One source per bank, so a broken feed doesn't stop the others. Titles are templated
("Minutes of ...", "Foreign Currency Reserves ... Market Notice"), so dedup is by guid/URL only.
Noise (routine operations, statistics) is filtered from the LLM with `llm.exclude` in config.yaml.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from trade_news.collectors.base import Batch, Context, RawItem, collector

# The BoE answers 403 to httpx's default User-Agent.
USER_AGENT = "trade-news/1.0 (+https://github.com/zotochev/trade-news)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def parse_rss(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml.lstrip(b"\xef\xbb\xbf \t\r\n"))
    out = []
    for it in root.iter("item"):
        link = _text(it, "link")
        out.append(
            {
                "guid": _text(it, "guid") or link,
                "title": _text(it, "title"),
                "url": link,
                "description": _text(it, "description"),
                "category": _text(it, "category"),
                "pub_date": _text(it, "pubDate"),
            }
        )
    return out


def _text(el: ET.Element, tag: str) -> str | None:
    raw = el.findtext(tag)
    if raw is None:
        return None
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(raw))).strip()
    return text or None


def to_raw_item(entry: dict, bank: str, hint: str, cutoff: datetime) -> RawItem | None:
    if not entry["guid"] or not entry["title"]:
        return None
    body = entry["description"]
    if body == entry["title"]:  # the Fed repeats the title as the description
        body = None
    published = parsedate_to_datetime(entry["pub_date"]) if entry["pub_date"] else None
    raw = {**entry, "bank": bank, "hint": hint}
    if published is not None and published < cutoff:
        # The feed always returns weeks of history: on the first run (or after downtime) old
        # releases would otherwise reach the LLM and Telegram as fresh news.
        raw["llm_skip"] = True
    return RawItem(
        source_item_id=entry["guid"],
        title=entry["title"],
        body=body,
        url=entry["url"],
        published_at=published,
        raw=raw,
    )


def _fetch(ctx: Context, bank: str, hint: str, default_feeds: list[str]) -> Batch:
    cutoff = ctx.now() - timedelta(hours=float(ctx.params.get("max_age_hours", 24)))
    items: list[RawItem] = []
    for url in ctx.params.get("feeds", default_feeds):
        entries = parse_rss(ctx.get(url, headers={"User-Agent": USER_AGENT}).content)
        items.extend(it for e in entries if (it := to_raw_item(e, bank, hint, cutoff)))
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
