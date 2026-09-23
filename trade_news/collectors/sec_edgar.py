"""SEC EDGAR latest filings (Atom feed of browse-edgar?action=getcurrent).

Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
Fair access: <= 10 req/s per user, User-Agent must be "Company/Name email@domain".

The feed is newest-first, 100 entries per page. We page until we reach entries older than
the newest `updated` seen last time (cursor per form type). Form 4 appears twice per filing
(Issuer and Reporting owner entries share one accession number); we keep the Issuer entry.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime

from trade_news.collectors.base import Batch, Context, RawItem, collector

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
PAGE_SIZE = 100
NS = {"a": "http://www.w3.org/2005/Atom"}
_TAG_RE = re.compile(r"<[^>]+>")
_ACCESSION_RE = re.compile(r"accession-number=([\d-]+)")
_TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<company>.+) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)$")


def parse_feed(xml: bytes | str) -> list[dict]:
    root = ET.fromstring(xml)
    entries = []
    for e in root.findall("a:entry", NS):
        title = (e.findtext("a:title", "", NS) or "").strip()
        summary_html = html.unescape(e.findtext("a:summary", "", NS) or "")
        link = e.find("a:link", NS)
        category = e.find("a:category", NS)
        acc = _ACCESSION_RE.search(e.findtext("a:id", "", NS) or "")
        m = _TITLE_RE.match(title)
        entries.append(
            {
                "accession": acc.group(1) if acc else None,
                "title": title,
                "form": category.get("term") if category is not None else (m and m["form"]),
                "company": m["company"] if m else None,
                "cik": m["cik"] if m else None,
                "role": m["role"] if m else None,
                "url": link.get("href") if link is not None else None,
                "updated": datetime.fromisoformat(e.findtext("a:updated", "", NS)),
                "summary": _summary_text(summary_html),
            }
        )
    return entries


def form_matches(actual: str | None, wanted: str) -> bool:
    """The feed's `type` filter is a prefix match (type=4 returns 424B2, 485BPOS, ...).
    Keep the exact form and its amendments only."""
    return actual is not None and (actual == wanted or actual == f"{wanted}/A")


def _summary_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    lines = (" ".join(_TAG_RE.sub("", ln).split()) for ln in s.splitlines())
    return "\n".join(ln for ln in lines if ln)


def to_raw_items(entries: list[dict]) -> list[RawItem]:
    by_acc: dict[str, dict] = {}
    for e in entries:
        if not e["accession"]:
            continue
        prev = by_acc.get(e["accession"])
        # For Form 4 prefer the Issuer entry: the company is what the news is about.
        if prev is None or (e["role"] == "Issuer" and prev["role"] != "Issuer"):
            by_acc[e["accession"]] = e
    return [
        RawItem(
            source_item_id=acc,
            title=e["title"],
            body=e["summary"],
            url=e["url"],
            published_at=e["updated"],
            raw={**e, "updated": e["updated"].isoformat()},
        )
        for acc, e in by_acc.items()
    ]


@collector("sec_edgar", secrets=("SEC_USER_AGENT",), title_dedup=False)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    cursor = dict(cursor or {})
    headers = {"User-Agent": ctx.secrets["SEC_USER_AGENT"], "Accept-Encoding": "gzip, deflate"}
    forms = ctx.params.get("forms", ["8-K", "10-Q", "10-K", "4"])
    max_pages = int(ctx.params.get("max_pages", 5))
    entries: list[dict] = []

    for form in forms:
        seen_until = datetime.fromisoformat(cursor[form]) if form in cursor else None
        newest = seen_until
        for page in range(max_pages):
            resp = ctx.get(
                FEED_URL,
                params={
                    "action": "getcurrent",
                    "type": form,
                    "owner": "include",
                    "start": page * PAGE_SIZE,
                    "count": PAGE_SIZE,
                    "output": "atom",
                },
                headers=headers,
            )
            page_entries = parse_feed(resp.content)
            entries.extend(e for e in page_entries if form_matches(e["form"], form))
            for e in page_entries:
                if newest is None or e["updated"] > newest:
                    newest = e["updated"]
            reached_known = seen_until is not None and any(
                e["updated"] < seen_until for e in page_entries
            )
            if reached_known or len(page_entries) < PAGE_SIZE or seen_until is None:
                break  # first run: one page only, no deep backfill
        else:
            ctx.log.warning("edgar_max_pages_reached", form=form, max_pages=max_pages)
        if newest is not None:
            cursor[form] = newest.isoformat()

    return Batch(items=to_raw_items(entries), cursor=cursor)
