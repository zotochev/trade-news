"""Background digest ("Фон рынка"): news below the delivery thresholds, grouped by industry.

A few times a day (background_digest.hours_utc) every recipient gets one digest of the items
first annotated since the previous one whose main link has importance >= min_importance but
which did not pass the delivery rules (those went out on their own). Inside an industry the
items of one asset become one line: an arrow per item and a link to the most important one.
The end of the last window is kept in `settings`, so a restart neither repeats nor skips.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa
import structlog

from trade_news import delivery, sectors, views
from trade_news.config import BackgroundConfig
from trade_news.db.schema import annotations, items, raw_items, settings
from trade_news.http import RateLimiter
from trade_news.telegram import subscribers as subs
from trade_news.telegram.api import Api, TelegramError
from trade_news.telegram.message import ARROW, MAX_LEN, _label

log = structlog.get_logger()

SETTINGS_KEY = "background_digest"
NO_SECTOR = "Без отрасли"
OTHER = "Другие отрасли"  # industries with a single item, merged to save headers
SUMMARY_CHARS = 110
MORE_LABELS = 8


@dataclass
class AssetLine:
    label: str
    tag: str = ""  # shown in the line itself (OTHER section)
    arrows: list[str] = field(default_factory=list)
    best: dict | None = None  # the item with the highest importance (newest on a tie)


@dataclass
class Section:
    title: str  # "#полупроводники" or NO_SECTOR
    count: int = 0
    directions: Counter = field(default_factory=Counter)
    assets: dict[str, AssetLine] = field(default_factory=dict)


# --- selection ------------------------------------------------------------------------


def window_items(
    conn: sa.Connection, start: datetime, end: datetime, min_importance: int
) -> list[dict]:
    """Items first annotated in [start, end) that are background: main link importance >=
    min_importance and not passing the delivery rules. Each dict has links and sectors."""
    first = (
        sa.select(annotations.c.item_id)
        .group_by(annotations.c.item_id)
        .having(sa.func.min(annotations.c.created_at) >= start)
        .having(sa.func.min(annotations.c.created_at) < end)
    )
    rows = conn.execute(
        sa.select(
            items.c.id,
            items.c.source,
            items.c.title,
            items.c.published_at,
            items.c.canonical_url,
            raw_items.c.url.label("raw_url"),
            views.SUMMARY.label("summary"),
            views.EVENT_TYPE.label("event_type"),
        )
        .join(annotations, sa.and_(annotations.c.item_id == items.c.id, views.current_annotation()))
        .join(raw_items, raw_items.c.id == items.c.raw_item_id)
        .where(items.c.id.in_(first))
        .order_by(items.c.published_at, items.c.id)
    ).mappings()
    candidates = [dict(r) for r in rows]
    ids = [r["id"] for r in candidates]
    links, secs = views.links_by_item(conn, ids), views.sectors_by_item(conn, ids)
    rules = delivery.load_rules(conn)
    out = []
    for r in candidates:
        r["links"] = links.get(r["id"], [])
        r["sectors"] = secs.get(r["id"], [])
        main = main_link(r["links"])
        if main is None or (main.get("importance") or 0) < min_importance:
            continue
        if delivery.passes(rules, r["links"], r["event_type"], r["source"]):
            continue  # already delivered on its own
        out.append(r)
    return out


def main_link(links: list[dict]) -> dict | None:
    primary = [x for x in links if x.get("is_primary")] or links
    return max(primary, key=lambda x: x.get("importance") or 0) if primary else None


# --- grouping and formatting ----------------------------------------------------------


def group(found: list[dict]) -> list[Section]:
    """Sections by the item's first industry (or sector), biggest first, NO_SECTOR last."""
    by_title: dict[str, Section] = {}
    for item in found:
        pairs = item["sectors"]
        title = f"#{sectors.HASHTAGS[pairs[0][1] or pairs[0][0]]}" if pairs else NO_SECTOR
        sec = by_title.setdefault(title, Section(title))
        main = main_link(item["links"])
        assert main is not None  # window_items keeps only items with a main link
        sec.count += 1
        sec.directions[main.get("direction")] += 1
        label = _label(main)
        line = sec.assets.setdefault(label, AssetLine(label))
        line.arrows.append(ARROW.get(main.get("direction"), "●"))
        imp = main.get("importance") or 0
        if line.best is None or imp >= (main_link(line.best["links"]) or {}).get("importance", 0):
            line.best = item
    big = [s for s in by_title.values() if s.count > 1 or s.title == NO_SECTOR]
    other = Section(OTHER)
    for s in by_title.values():
        if s.count == 1 and s.title != NO_SECTOR:
            other.count += 1
            other.directions += s.directions
            for label, line in s.assets.items():
                line.tag = s.title
                other.assets[f"{s.title} {label}"] = line
    if other.count:
        big.append(other)
    order = {NO_SECTOR: 2, OTHER: 1}
    return sorted(big, key=lambda s: (order.get(s.title, 0), -s.count, s.title))


def _balance(directions: Counter) -> str:
    parts = [f"{ARROW[d]}{directions[d]}" for d in ("bullish", "bearish") if directions[d]]
    return " ".join(parts)


def _link(item: dict) -> str:
    text = item.get("summary") or item.get("title") or ""
    if len(text) > SUMMARY_CHARS:
        text = text[: SUMMARY_CHARS - 1].rstrip() + "…"
    text = html.escape(text)
    url = item.get("raw_url") or item.get("canonical_url")
    if url and url.startswith(("https://", "http://")):
        return f'<a href="{html.escape(url, quote=True)}">{text}</a>'
    return text


def format_sections(
    sections: list[Section], start: datetime, end: datetime, max_assets: int
) -> list[str]:
    total = sum(s.count for s in sections)
    head = (
        f"🗂 <b>Фон рынка</b> · {start:%H:%M}–{end:%H:%M} UTC · {total} "
        f"{_plural(total, 'новость', 'новости', 'новостей')}\n"
        "<i>Новости ниже порога рассылки, по отраслям</i>"
    )
    blocks = []
    for sec in sections:
        lines = [
            f"<b>{html.escape(sec.title)}</b> · {sec.count} {_balance(sec.directions)}".rstrip()
        ]
        assets = sorted(
            sec.assets.values(),
            key=lambda a: (-len(a.arrows), -_importance(a), a.label),
        )
        limit = max_assets if sec.title != OTHER else max_assets * 2
        for a in assets[:limit]:
            n = len(a.arrows)
            arrows = "".join(a.arrows[:6]) + ("…" if n > 6 else "")
            count = f" ({n})" if n > 1 else ""
            assert a.best is not None
            tag = f" {html.escape(a.tag)}" if a.tag else ""
            lines.append(f"{html.escape(a.label)} {arrows}{count} — {_link(a.best)}{tag}")
        rest = assets[limit:]
        if rest:
            names = ", ".join(html.escape(a.label) for a in rest[:MORE_LABELS])
            more = "…" if len(rest) > MORE_LABELS else ""
            lines.append(f"<i>ещё {len(rest)}: {names}{more}</i>")
        blocks.append("\n".join(lines))
    return _pack(head, blocks)


def _importance(a: AssetLine) -> int:
    return (main_link(a.best["links"]) or {}).get("importance", 0) if a.best else 0


def visible_len(text: str) -> int:
    """Telegram's 4096 limit counts the text after parsing the HTML, not the markup."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _pack(head: str, blocks: list[str]) -> list[str]:
    messages, current = [], head
    for block in blocks:
        if visible_len(current) + 2 + visible_len(block) > MAX_LEN:
            messages.append(current)
            current = block
        else:
            current += "\n\n" + block
    messages.append(current)
    return messages


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# --- the job --------------------------------------------------------------------------


def window(conn: sa.Connection, cfg: BackgroundConfig, now: datetime) -> tuple[datetime, datetime]:
    """From the end of the previous digest (at most max_window_hours back) to now."""
    value = conn.execute(sa.select(settings.c.value).where(settings.c.key == SETTINGS_KEY)).scalar()
    earliest = now - timedelta(hours=cfg.max_window_hours)
    start = earliest
    if value and value.get("until"):
        start = max(earliest, datetime.fromisoformat(value["until"]))
    return start, now


def _save_until(conn: sa.Connection, until: datetime, now: datetime) -> None:
    value = {"until": until.isoformat()}
    updated = conn.execute(
        settings.update().where(settings.c.key == SETTINGS_KEY).values(value=value, updated_at=now)
    ).rowcount
    if not updated:
        conn.execute(settings.insert().values(key=SETTINGS_KEY, value=value, updated_at=now))


def build(engine: sa.Engine, cfg: BackgroundConfig, now: datetime) -> tuple[list[str], datetime]:
    """(messages, window end); no messages when the window is empty."""
    with engine.connect() as conn:
        start, end = window(conn, cfg, now)
        found = window_items(conn, start, end, cfg.min_importance)
    if not found:
        return [], end
    return format_sections(group(found), start, end, cfg.max_assets_per_sector), end


def run_background(
    engine: sa.Engine,
    api: Api,
    root_chat_id: int | None,
    cfg: BackgroundConfig,
    now: Callable[[], datetime],
) -> int:
    """Sends the digest to every recipient. Returns messages sent."""
    ts = now()
    with engine.connect() as conn:
        if not delivery.load_rules(conn).enabled:
            return 0
        recipients = subs.recipients(conn, root_chat_id)
    messages, end = build(engine, cfg, ts)
    sent = 0
    per_chat = RateLimiter(calls=1, period=1.1)
    for chat_id in recipients if messages else []:
        for text in messages:
            per_chat.acquire()
            try:
                api(
                    "sendMessage",
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                sent += 1
            except TelegramError as exc:
                if exc.chat_unreachable and chat_id != root_chat_id:
                    with engine.begin() as conn:
                        subs.unsubscribe(conn, chat_id, "blocked", ts)
                log.warning("background_send_failed", chat_id=chat_id, error=str(exc))
                break
    with engine.begin() as conn:
        _save_until(conn, end, ts)
    log.info("background_digest_done", messages=len(messages), sent=sent)
    return sent
