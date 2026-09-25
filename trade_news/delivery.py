"""Telegram delivery (stage 4): which annotated items go out, to whom, without repeats.

Rules live in the `settings` table (key "delivery_rules") and are edited on the admin page.
Delivery is OFF until enabled there.

An item passes when its main link (the primary one with the highest importance) meets the
thresholds, or when any of its links is a watchlist asset at the watchlist threshold.
Every (item, chat) pair gets one `deliveries` row: pending → sent | failed | skipped. The row is
checked before sending and updated after, so nothing is sent twice; Telegram being down leaves
rows pending for the next run.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
import structlog
from pydantic import BaseModel, Field

from trade_news import views
from trade_news.db.engine import insert_ignore
from trade_news.db.schema import annotations, deliveries, items, settings
from trade_news.http import RateLimiter
from trade_news.telegram import subscribers as subs
from trade_news.telegram.api import Api, TelegramError
from trade_news.telegram.message import format_digest, format_item

log = structlog.get_logger()

SETTINGS_KEY = "delivery_rules"
ASSET_CLASSES = ["equity", "fx", "crypto", "commodity", "index", "rates", "macro"]
EVENT_TYPES = [
    "earnings", "guidance", "m_and_a", "regulatory", "macro", "rate_decision", "cb_speech",
    "insider", "other",
]  # fmt: skip
MATERIAL_EVENTS = [
    "earnings", "guidance", "m_and_a", "regulatory", "macro", "rate_decision", "cb_speech",
    "insider",
]  # fmt: skip
MAX_ATTEMPTS = 5
ALWAYS_SOURCES = ["ff_calendar", "cme_fedwatch", "fred"]


class DeliveryRules(BaseModel):
    enabled: bool = False
    min_importance: int = Field(default=3, ge=1, le=5)
    require_direction: bool = True  # skip neutral news
    event_types: list[str] = Field(default_factory=lambda: list(MATERIAL_EVENTS))  # empty: any
    asset_classes: list[str] = Field(default_factory=lambda: list(ASSET_CLASSES))  # empty: any
    watchlist: list[str] = Field(default_factory=list)  # symbols with their own threshold
    watchlist_min_importance: int = Field(default=3, ge=1, le=5)
    max_per_hour: int = Field(default=6, ge=1, le=60)  # per chat; the rest waits
    max_age_hours: float = Field(default=6, gt=0, le=72)  # older news is never sent
    # Structured signals are rare and already filtered by the collector's own thresholds
    # (a release with an actual, a big FedWatch or yield move): they skip the importance and
    # direction thresholds and go first.
    always_sources: list[str] = Field(default_factory=lambda: list(ALWAYS_SOURCES))
    always_event_types: list[str] = Field(default_factory=lambda: ["rate_decision"])


def load_rules(conn: sa.Connection) -> DeliveryRules:
    value = conn.execute(sa.select(settings.c.value).where(settings.c.key == SETTINGS_KEY)).scalar()
    return DeliveryRules.model_validate(value or {})


def save_rules(conn: sa.Connection, rules: DeliveryRules, now: datetime) -> None:
    value = rules.model_dump()
    updated = conn.execute(
        settings.update().where(settings.c.key == SETTINGS_KEY).values(value=value, updated_at=now)
    ).rowcount
    if not updated:
        conn.execute(settings.insert().values(key=SETTINGS_KEY, value=value, updated_at=now))


def is_priority(rules: DeliveryRules, source: str | None, event_type: str | None) -> bool:
    return source in rules.always_sources or event_type in rules.always_event_types


def passes(
    rules: DeliveryRules, links: list[dict], event_type: str | None, source: str | None = None
) -> bool:
    if is_priority(rules, source, event_type):
        return True
    watch = {s.upper() for s in rules.watchlist}
    if any(
        (link.get("symbol") or "").upper() in watch
        and (link.get("importance") or 0) >= rules.watchlist_min_importance
        for link in links
    ):
        return True
    primary = [link for link in links if link.get("is_primary")] or links
    if not primary:
        return False
    main = max(primary, key=lambda link: link.get("importance") or 0)
    if (main.get("importance") or 0) < rules.min_importance:
        return False
    if rules.require_direction and main.get("direction") in (None, "neutral"):
        return False
    if rules.event_types and event_type not in rules.event_types:
        return False
    return not rules.asset_classes or main.get("asset_class") in rules.asset_classes


def matching_items(
    conn: sa.Connection, rules: DeliveryRules, since: datetime
) -> tuple[list[int], int, set[int]]:
    """(ids of items annotated since `since` that pass the rules, priority signals first, then
    oldest first; total annotated; ids of the priority ones)."""
    rows = conn.execute(
        sa.select(annotations.c.item_id, views.EVENT_TYPE.label("event_type"), items.c.source)
        .join(items, items.c.id == annotations.c.item_id)
        .where(views.current_annotation(), annotations.c.created_at >= since)
        .order_by(annotations.c.created_at)
    ).all()
    links = views.links_by_item(conn, [r.item_id for r in rows])
    ok = [r for r in rows if passes(rules, links.get(r.item_id, []), r.event_type, r.source)]
    # priority signals first: the per-hour cap must not hold them behind ordinary news
    ok.sort(key=lambda r: not is_priority(rules, r.source, r.event_type))
    priority = {r.item_id for r in ok if is_priority(rules, r.source, r.event_type)}
    return [r.item_id for r in ok], len(rows), priority


# --- sending ---------------------------------------------------------------------------


@dataclass(slots=True)
class DeliveryStats:
    candidates: int = 0
    sent: int = 0  # items
    messages: int = 0
    failed: int = 0
    deferred: int = 0  # items over the per-hour cap, will go later if still fresh
    skipped: int = 0  # became too old while waiting
    stopped: str | None = None


def run_delivery(
    engine: sa.Engine,
    api: Api,
    root_chat_id: int | None,
    now: Callable[[], datetime],
) -> DeliveryStats:
    """Priority signals go one per message; the other items of a run go as one digest (one
    item alone is sent as a normal message). The per-hour cap counts messages."""
    stats = DeliveryStats()
    ts = now()
    with engine.connect() as conn:
        rules = load_rules(conn)
    if not rules.enabled:
        return stats
    cutoff = ts - timedelta(hours=rules.max_age_hours)
    with engine.begin() as conn:
        stats.skipped = _skip_stale(conn, cutoff)
        item_ids, _, priority = matching_items(conn, rules, cutoff)
        recipients = subs.recipients(conn, root_chat_id)
    stats.candidates = len(item_ids)
    if not item_ids or not recipients:
        return stats

    global_limit = RateLimiter(calls=20, period=1)  # Telegram: ~30 msg/s per bot overall
    for chat_id in recipients:
        per_chat = RateLimiter(calls=1, period=1.1)  # and about 1 msg/s into one chat
        with engine.connect() as conn:
            done = set(
                conn.execute(
                    sa.select(deliveries.c.item_id).where(
                        deliveries.c.channel == str(chat_id),
                        deliveries.c.status.in_(("sent", "failed", "skipped")),
                    )
                ).scalars()
            )
            sent_last_hour = conn.execute(
                sa.select(sa.func.count(sa.distinct(deliveries.c.message_id))).where(
                    deliveries.c.channel == str(chat_id),
                    deliveries.c.status == "sent",
                    deliveries.c.sent_at >= ts - timedelta(hours=1),
                )
            ).scalar()
        todo = [i for i in item_ids if i not in done]
        if not todo:
            continue
        messages = _plan(engine, chat_id, todo, priority, now)
        budget = max(0, rules.max_per_hour - sent_last_hour)
        stats.deferred += sum(len(ids) for ids, _ in messages[budget:])
        for ids, text in messages[:budget]:
            global_limit.acquire()
            per_chat.acquire()
            outcome = _send(engine, api, chat_id, ids, text, root_chat_id, now)
            if outcome == "sent":
                stats.sent += len(ids)
                stats.messages += 1
            elif outcome == "failed":
                stats.failed += len(ids)
            elif outcome == "chat_gone":
                stats.failed += len(ids)
                break
            else:  # "stop": Telegram unavailable, try again next run
                stats.stopped = "telegram_unavailable"
                log.warning("delivery_stopped", **asdict(stats))
                return stats
    if stats.sent or stats.failed or stats.deferred or stats.skipped:
        log.info("delivery_done", **asdict(stats))
    return stats


def _plan(
    engine, chat_id: int, todo: list[int], priority: set[int], now
) -> list[tuple[list[int], str]]:
    """Messages for one chat: [(item ids, text)], priority ones first. Creates the pending
    delivery rows, so nothing planned is lost if Telegram fails midway."""
    with engine.begin() as conn:
        insert_ignore(
            conn,
            deliveries,
            [
                {"item_id": i, "channel": str(chat_id), "status": "pending", "created_at": now()}
                for i in todo
            ],
            "item_id",
            "channel",
        )
        loaded = {i: views.news_item(conn, i) for i in todo}
        gone = [i for i, item in loaded.items() if item is None]
        if gone:
            conn.execute(
                deliveries.update()
                .where(deliveries.c.item_id.in_(gone), deliveries.c.channel == str(chat_id))
                .values(status="skipped")
            )
    items = [(i, loaded[i]) for i in todo if loaded[i] is not None]
    messages = [([i], format_item(item)) for i, item in items if i in priority]
    rest = [item for i, item in items if i not in priority]
    if len(rest) == 1:
        messages.append(([rest[0]["id"]], format_item(rest[0])))
    elif rest:
        messages += format_digest(rest)
    return messages


def _skip_stale(conn, cutoff: datetime) -> int:
    """Pending rows whose item was annotated before the age cutoff are given up."""
    stale = sa.select(annotations.c.item_id).where(annotations.c.created_at < cutoff)
    return conn.execute(
        deliveries.update()
        .where(deliveries.c.status == "pending", deliveries.c.item_id.in_(stale))
        .values(status="skipped")
    ).rowcount


def _send(engine, api: Api, chat_id: int, ids: list[int], text: str, root_chat_id, now) -> str:
    row = deliveries.c.item_id.in_(ids) & (deliveries.c.channel == str(chat_id))
    try:
        try:
            result = api(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            if exc.error_code != 400 or exc.chat_unreachable:
                raise
            # Telegram rejected the HTML: send the same text without markup rather than nothing
            plain = re.sub(r"<[^>]+>", "", text)
            result = api("sendMessage", chat_id=chat_id, text=plain, disable_web_page_preview=True)
    except TelegramError as exc:
        with engine.begin() as conn:
            if exc.chat_unreachable:
                conn.execute(
                    deliveries.update()
                    .where(row)
                    .values(status="failed", last_error=str(exc)[:500])
                )
                if chat_id != root_chat_id:
                    subs.unsubscribe(conn, chat_id, "blocked", now())
                log.info("delivery_chat_unreachable", chat_id=chat_id, error=str(exc))
                return "chat_gone"
            if exc.error_code == 429 or exc.error_code >= 500:
                _bump(conn, row, str(exc))
                return "stop"
            _bump(conn, row, str(exc), give_up=True)
        log.warning("delivery_failed", chat_id=chat_id, item_ids=ids, error=str(exc))
        return "failed"
    except Exception as exc:  # network: Telegram unreachable
        with engine.begin() as conn:
            _bump(conn, row, f"{type(exc).__name__}: {exc}"[:500])
        log.warning("delivery_transport_error", error=repr(exc))
        return "stop"

    with engine.begin() as conn:
        conn.execute(
            deliveries.update()
            .where(row)
            .values(
                status="sent", sent_at=now(), message_id=str((result or {}).get("message_id", ""))
            )
        )
    return "sent"


def _bump(conn, row, error: str, give_up: bool = False) -> None:
    attempts = conn.execute(sa.select(sa.func.max(deliveries.c.attempts)).where(row)).scalar() or 0
    status = "failed" if give_up or attempts + 1 >= MAX_ATTEMPTS else "pending"
    conn.execute(
        deliveries.update()
        .where(row)
        .values(attempts=attempts + 1, last_error=error[:500], status=status)
    )
