"""Cross-source deduplication.

Order of checks within ±window of published_at: normalized URL → exact normalized title
hash → fuzzy title match. Originals are never deleted: duplicates point to the group leader
through items.dedup_group_id.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from rapidfuzz import fuzz, process

from trade_news.config import DedupConfig
from trade_news.db.schema import items

_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid",
    "ref", "ref_src", "cmpid", "ncid", "sr_share", "smid", "guccounter", "guce_referrer",
}  # fmt: skip
_TRACKING_PREFIXES = ("utm_", "__")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return url.strip()
    host = parts.hostname or ""
    host = host.removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host.lower(), path, urlencode(query), ""))


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", title).casefold()
    t = _NON_WORD.sub(" ", t)
    return " ".join(t.split())


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(title: str | None, body: str | None) -> str:
    return sha256(f"{normalize_title(title)}\n{' '.join((body or '').split())}")


@dataclass(frozen=True, slots=True)
class Match:
    group_id: int
    reason: str  # url | title_hash | fuzzy


def find_duplicate(
    conn: sa.Connection,
    *,
    canonical_url: str | None,
    title_norm: str,
    published_at: datetime,
    use_title: bool,
    cfg: DedupConfig,
    exclude_id: int | None = None,
    fuzzy: bool = True,
) -> Match | None:
    window = timedelta(hours=cfg.window_hours)
    base = sa.select(items.c.id, items.c.dedup_group_id, items.c.title_norm).where(
        items.c.published_at.between(published_at - window, published_at + window)
    )
    if exclude_id is not None:
        base = base.where(items.c.id != exclude_id)

    def first(q, reason: str) -> Match | None:
        row = conn.execute(q.order_by(items.c.id).limit(1)).first()
        return Match(row.dedup_group_id or row.id, reason) if row else None

    if canonical_url and (m := first(base.where(items.c.canonical_url == canonical_url), "url")):
        return m
    if not use_title or len(title_norm) < cfg.min_title_len:
        return None
    if m := first(base.where(items.c.title_hash == sha256(title_norm)), "title_hash"):
        return m
    if not fuzzy:
        return None

    # Only compare against sources that also allow title dedup: templated titles of primary
    # data (e.g. "8-K - Apple Inc. (...)") must not absorb news items.
    candidates = conn.execute(
        base.where(items.c.title_norm.is_not(None), items.c.title_hash.is_not(None))
    ).all()
    choices = {r.id: r.title_norm for r in candidates if len(r.title_norm) >= cfg.min_title_len}
    best = process.extractOne(
        title_norm, choices, scorer=fuzz.token_sort_ratio, score_cutoff=cfg.fuzzy_threshold
    )
    if best is None:
        return None
    _, _, row_id = best
    group = next(r.dedup_group_id or r.id for r in candidates if r.id == row_id)
    return Match(group, "fuzzy")
