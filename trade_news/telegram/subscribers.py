"""Subscriber list. Subscribing is immediate (no approval); rows are kept on unsubscribe."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa

from trade_news.db.schema import subscribers as t


def subscribe(
    conn: sa.Connection, chat_id: int, chat_type: str | None, title: str | None, now: datetime
) -> bool:
    """Returns True if the chat was not an active subscriber before."""
    row = conn.execute(sa.select(t.c.is_active).where(t.c.chat_id == chat_id)).first()
    values = {"chat_type": chat_type, "title": title, "is_active": True}
    if row is None:
        conn.execute(t.insert().values(chat_id=chat_id, subscribed_at=now, **values))
        return True
    if row.is_active:
        conn.execute(t.update().where(t.c.chat_id == chat_id).values(**values))
        return False
    conn.execute(
        t.update()
        .where(t.c.chat_id == chat_id)
        .values(subscribed_at=now, unsubscribed_at=None, unsubscribe_reason=None, **values)
    )
    return True


def unsubscribe(conn: sa.Connection, chat_id: int, reason: str, now: datetime) -> bool:
    """reason: stop (user asked) | blocked (bot blocked/kicked). True if it was active."""
    res = conn.execute(
        t.update()
        .where(t.c.chat_id == chat_id, t.c.is_active.is_(True))
        .values(is_active=False, unsubscribed_at=now, unsubscribe_reason=reason)
    )
    return res.rowcount > 0


def is_active(conn: sa.Connection, chat_id: int) -> bool:
    return bool(conn.execute(sa.select(t.c.is_active).where(t.c.chat_id == chat_id)).scalar())


def active_chat_ids(conn: sa.Connection) -> list[int]:
    return list(conn.execute(sa.select(t.c.chat_id).where(t.c.is_active.is_(True))).scalars())


def recipients(conn: sa.Connection, root_chat_id: int | None) -> list[int]:
    """Who gets the mailing: every active subscriber plus the owner (always)."""
    ids = active_chat_ids(conn)
    if root_chat_id is not None and root_chat_id not in ids:
        ids.insert(0, root_chat_id)
    return ids
