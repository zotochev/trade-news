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
from trade_news.db.schema import annotations, deliveries, settings
from trade_news.http import RateLimiter
from trade_news.telegram import subscribers as subs
from trade_news.telegram.api import Api, TelegramError
from trade_news.telegram.message import format_item

log = structlog.get_logger()

SETTINGS_KEY = "delivery_rules"
ASSET_CLASSES = ["equity", "fx", "crypto", "commodity", "index", "rates", "macro"]
EVENT_TYPES = [
    "earnings", "guidance", "m_and_a", "regulatory", "macro", "rate_decision", "cb_speech",
    "insider", "other",
]  # fmt: skip
MATERIAL_EVENTS = [
    "earnings",
    "guidance",
    "m_and_a",
    "regulatory",
    "macro",
    "rate_decision",
    "cb_speech",
]
MAX_ATTEMPTS = 5


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


def passes(rules: DeliveryRules, links: list[dict], event_type: str | None) -> bool:
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
) -> tuple[list[int], int]:
    """(ids of items annotated since `since` that pass the rules, oldest first; total annotated)."""
    rows = conn.execute(
        sa.select(annotations.c.item_id, views.EVENT_TYPE.label("event_type"))
        .where(views.current_annotation(), annotations.c.created_at >= since)
        .order_by(annotations.c.created_at)
    ).all()
    links = views.links_by_item(conn, [r.item_id for r in rows])
    ok = [r.item_id for r in rows if passes(rules, links.get(r.item_id, []), r.event_type)]
    return ok, len(rows)


# --- sending ---------------------------------------------------------------------------


@dataclass(slots=True)
class DeliveryStats:
    candidates: int = 0
    sent: int = 0
    failed: int = 0
    deferred: int = 0  # over the per-hour cap, will go later if still fresh
    skipped: int = 0  # became too old while waiting
    stopped: str | None = None


def run_delivery(
    engine: sa.Engine,
    api: Api,
    root_chat_id: int | None,
    now: Callable[[], datetime],
) -> DeliveryStats:
    stats = DeliveryStats()
    ts = now()
    with engine.connect() as conn:
        rules = load_rules(conn)
    if not rules.enabled:
        return stats
    cutoff = ts - timedelta(hours=rules.max_age_hours)
    with engine.begin() as conn:
        stats.skipped = _skip_stale(conn, cutoff)
        item_ids, _ = matching_items(conn, rules, cutoff)
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
                sa.select(sa.func.count()).where(
                    deliveries.c.channel == str(chat_id),
                    deliveries.c.status == "sent",
                    deliveries.c.sent_at >= ts - timedelta(hours=1),
                )
            ).scalar()
        todo = [i for i in item_ids if i not in done]
        budget = max(0, rules.max_per_hour - sent_last_hour)
        stats.deferred += max(0, len(todo) - budget)
        for item_id in todo[:budget]:
            global_limit.acquire()
            per_chat.acquire()
            outcome = _send_one(engine, api, chat_id, item_id, root_chat_id, now)
            if outcome == "sent":
                stats.sent += 1
            elif outcome == "failed":
                stats.failed += 1
            elif outcome == "chat_gone":
                stats.failed += 1
                break
            else:  # "stop": Telegram unavailable, try again next run
                stats.stopped = "telegram_unavailable"
                log.warning("delivery_stopped", **asdict(stats))
                return stats
    if stats.sent or stats.failed or stats.deferred or stats.skipped:
        log.info("delivery_done", **asdict(stats))
    return stats


def _skip_stale(conn, cutoff: datetime) -> int:
    """Pending rows whose item was annotated before the age cutoff are given up."""
    stale = sa.select(annotations.c.item_id).where(annotations.c.created_at < cutoff)
    return conn.execute(
        deliveries.update()
        .where(deliveries.c.status == "pending", deliveries.c.item_id.in_(stale))
        .values(status="skipped")
    ).rowcount


def _send_one(engine, api: Api, chat_id: int, item_id: int, root_chat_id, now) -> str:
    with engine.begin() as conn:
        insert_ignore(
            conn,
            deliveries,
            [
                {
                    "item_id": item_id,
                    "channel": str(chat_id),
                    "status": "pending",
                    "created_at": now(),
                }
            ],
            "item_id",
            "channel",
        )
        item = views.news_item(conn, item_id)
    row = (deliveries.c.item_id == item_id) & (deliveries.c.channel == str(chat_id))
    if item is None:
        with engine.begin() as conn:
            conn.execute(deliveries.update().where(row).values(status="skipped"))
        return "failed"

    text = format_item(item)
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
        log.warning("delivery_failed", chat_id=chat_id, item_id=item_id, error=str(exc))
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
    attempts = conn.execute(sa.select(deliveries.c.attempts).where(row)).scalar() or 0
    status = "failed" if give_up or attempts + 1 >= MAX_ATTEMPTS else "pending"
    conn.execute(
        deliveries.update()
        .where(row)
        .values(attempts=attempts + 1, last_error=error[:500], status=status)
    )
