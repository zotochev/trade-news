"""Shared RSS 2.0 parsing for feed-based sources (no collectors here).

Feeds have no paging or `since` parameter: every run re-reads the whole feed and already-stored
guids are skipped by the raw store's unique key. A feed also returns days or weeks of history,
so entries older than a cutoff are stored with `llm_skip`: otherwise the first run (or the one
after downtime) would send old news to the LLM and Telegram as fresh.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from trade_news.collectors.base import Context, RawItem

# Some sites (the BoE) answer 403 to httpx's default User-Agent.
USER_AGENT = "trade-news/1.0 (+https://github.com/zotochev/trade-news)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def parse_rss(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml.lstrip(b"\xef\xbb\xbf \t\r\n"))
    out = []
    for it in root.iter("item"):
        link = _text(it, "link")
        categories = [c for el in it.findall("category") if (c := _clean(el.text))]
        out.append(
            {
                "guid": _text(it, "guid") or link,
                "title": _text(it, "title"),
                "url": link,
                "description": _text(it, "description"),
                "category": categories[0] if categories else None,
                "categories": categories,
                "pub_date": _text(it, "pubDate"),
            }
        )
    return out


def _text(el: ET.Element, tag: str) -> str | None:
    return _clean(el.findtext(tag))


def _clean(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(raw))).strip()
    return text or None


def fetch_entries(ctx: Context, url: str) -> list[dict]:
    return parse_rss(ctx.get(url, headers={"User-Agent": USER_AGENT}).content)


def cutoff(ctx: Context) -> datetime:
    return ctx.now() - timedelta(hours=float(ctx.params.get("max_age_hours", 24)))


def to_raw_item(entry: dict, cutoff: datetime, **extra: Any) -> RawItem | None:
    if not entry["guid"] or not entry["title"]:
        return None
    body = entry["description"]
    if body == entry["title"]:  # the Fed repeats the title as the description
        body = None
    published = parsedate_to_datetime(entry["pub_date"]) if entry["pub_date"] else None
    raw = {**entry, **extra}
    if published is not None and published < cutoff:
        raw["llm_skip"] = True
    return RawItem(
        source_item_id=entry["guid"],
        title=entry["title"],
        body=body,
        url=entry["url"],
        published_at=published,
        raw=raw,
    )
