"""SEC EDGAR latest filings (Atom feed of browse-edgar?action=getcurrent).

Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
Fair access: <= 10 req/s per user, User-Agent must be "Company/Name email@domain".

The feed is newest-first, 100 entries per page. We page until we reach entries older than
the newest `updated` seen last time (cursor per form type). Form 4 and Schedule 13D appear
twice per filing (the company and the filer share one accession number); we keep the
company's entry (role Issuer / Subject).

Form 4 and Schedule 13D get their details fetched once per filing (Form 4: the submission
text, 13D: primary_doc.xml); the accessions already fetched are kept in the cursor.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import datetime

from trade_news.collectors import form4, schedule13d
from trade_news.collectors.base import Batch, Context, RawItem, collector

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
PAGE_SIZE = 100
NS = {"a": "http://www.w3.org/2005/Atom"}
_TAG_RE = re.compile(r"<[^>]+>")
# cursor keys: accessions whose details were fetched, per form with details
SEEN_KEYS = {"4": "form4_seen", "SCHEDULE 13D": "13d_seen"}
FORM4_SEEN_KEY = SEEN_KEYS["4"]
SEEN_MAX = 3000  # several days of Form 4 (~400 a day): far beyond the feed pages we read
COMPANY_ROLES = ("Issuer", "Subject")
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
        # Prefer the company's entry: the company is what the news is about.
        if prev is None or (e["role"] in COMPANY_ROLES and prev["role"] not in COMPANY_ROLES):
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


@collector(
    "sec_edgar",
    secrets=("SEC_USER_AGENT",),
    description="SEC EDGAR: подачи 8-K, 10-Q, 10-K, Form 4 (инсайдеры), Schedule 13D (доли > 5%)",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    cursor = dict(cursor or {})
    cursor_before = set(cursor)
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

    first_run = [f for f in forms if f not in cursor_before]
    items = to_raw_items(entries)
    detailed = [
        f for f in SEEN_KEYS if f in forms and (f != "4" or ctx.params.get("form4_details", True))
    ]
    if detailed:
        seen = {f: set(cursor.get(SEEN_KEYS[f]) or []) for f in detailed}
        items = _with_details(ctx, items, headers, seen)
        for f in detailed:  # remember the filings fetched now (parsed or not), newest last
            done = [it.source_item_id for it in items if form_matches(it.raw.get("form"), f)]
            kept = [a for a in cursor.get(SEEN_KEYS[f]) or [] if a not in set(done)]
            cursor[SEEN_KEYS[f]] = (kept + done)[-SEEN_MAX:]
    if first_run and cursor_before:  # a form added to an existing source: its backlog
        items = [_backlog(it) if _form_of(it.raw, first_run) else it for it in items]
    return Batch(items=items, cursor=cursor)


def _form_of(raw: dict, forms: list[str]) -> str | None:
    return next((f for f in forms if form_matches(raw.get("form"), f)), None)


def _backlog(it: RawItem) -> RawItem:
    """The first page of a newly added form spans days: stored (13D history counts for later
    filings) but never sent to the LLM, or day-old filings would go out as fresh news."""
    return RawItem(it.source_item_id, it.title, it.body, it.url, it.published_at,
                   {**it.raw, "llm_skip": "first run backlog"})  # fmt: skip


def _detail_form(raw: dict) -> str | None:
    return next((f for f in SEEN_KEYS if form_matches(raw.get("form"), f)), None)


def _with_details(
    ctx: Context, items: list[RawItem], headers, seen: dict[str, set[str]]
) -> list[RawItem]:
    """Fetches and parses new Form 4 / Schedule 13D filings (`seen`: forms to detail, with the
    accessions already fetched). Items that don't matter get raw["llm_skip"].

    Filings already fetched are left out of the batch entirely: the feed repeats them on every
    run, and a copy without the parsed details has another content hash, so ingest would store
    it as a new revision over the parsed one. Over the per-run budget they are left out too,
    and not marked seen: the next run fetches them."""
    th = form4.Thresholds(**ctx.params.get("form4_thresholds", {}))
    budget = int(ctx.params.get("form4_max_details_per_run", 150))
    out = []
    for it in items:
        form = _detail_form(it.raw)
        if form not in seen:
            out.append(it)
            continue
        if it.source_item_id in seen[form] or budget <= 0:
            continue
        budget -= 1
        folder = it.url.rsplit("/", 1)[0]
        if form == "4":
            out.append(_form4_item(ctx, it, folder, headers, th))
        else:
            out.append(_schedule13d_item(ctx, it, folder, headers))
    return out


def _form4_item(ctx: Context, it: RawItem, folder: str, headers, th) -> RawItem:
    try:
        text = ctx.get(f"{folder}/{it.source_item_id}.txt", headers=headers).text
        parsed = form4.parse(text)
    except Exception as exc:  # one broken filing must not fail the whole batch
        ctx.log.warning("form4_fetch_failed", accession=it.source_item_id, error=repr(exc))
        parsed = None
    if parsed is None:
        return RawItem(it.source_item_id, it.title, it.body, it.url, it.published_at,
                       {**it.raw, "llm_skip": "form 4 not parsed"})  # fmt: skip
    significant, reason = form4.significance(parsed, th)
    # The same trade is often reported in several filings (e.g. a fund and its manager).
    # The headline is built from the facts, so an exact title match is a safe duplicate.
    raw = {**it.raw, "form4": asdict(parsed), "significance": reason, "dedup_title": "exact"}
    if not significant:
        raw["llm_skip"] = reason
    return RawItem(it.source_item_id, form4.headline(parsed), form4.describe(parsed), it.url,
                   it.published_at, raw)  # fmt: skip


def _schedule13d_item(ctx: Context, it: RawItem, folder: str, headers) -> RawItem:
    try:
        parsed = schedule13d.parse(ctx.get(f"{folder}/primary_doc.xml", headers=headers).text)
    except Exception as exc:
        ctx.log.warning("13d_fetch_failed", accession=it.source_item_id, error=repr(exc))
        parsed = None
    if parsed is None:
        return RawItem(it.source_item_id, it.title, it.body, it.url, it.published_at,
                       {**it.raw, "llm_skip": "13d not parsed"})  # fmt: skip
    return RawItem(it.source_item_id, schedule13d.headline(parsed), schedule13d.describe(parsed),
                   it.url, it.published_at, {**it.raw, "schedule13d": asdict(parsed)})  # fmt: skip
