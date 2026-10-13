"""Calendar page: the forced flows calendar for a few weeks, manual events and edits.

Events added here have source "manual"; an edited collector event is marked `edited`, so the
next collector run does not overwrite it. Token unlocks and central bank meetings come from
other tables (see flows/calendar.py) and are read-only here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import sqlalchemy as sa

from trade_news.db.schema import calendar_events
from trade_news.flows import calendar

WEEKS = (2, 4, 6, 8)
TIMEZONES = {"America/New_York": "Нью-Йорк", "Europe/Moscow": "Москва", "UTC": "UTC"}


@dataclass(frozen=True, slots=True)
class CalendarFilter:
    weeks: int = 6
    type: str = ""
    market: str = ""
    major: bool = False

    @classmethod
    def from_params(cls, p) -> CalendarFilter:
        try:
            weeks = int(p.get("weeks") or 6)
        except ValueError:
            weeks = 6
        return cls(
            weeks=weeks if weeks in WEEKS else 6,
            type=p.get("type", "") if p.get("type") in calendar.TYPES else "",
            market=p.get("market", "") if p.get("market") in calendar.MARKETS else "",
            major=p.get("major") == "1",
        )


def rows(conn, today: date, f: CalendarFilter) -> tuple[list[dict], int]:
    """(events, buyback blackouts hidden): dozens of windows at once in earnings season, shown
    only when that type is chosen."""
    found = calendar.events(
        conn,
        today,
        today + timedelta(weeks=f.weeks),
        [f.type] if f.type else None,
        [f.market] if f.market else None,
        f.major,
    )
    if f.type:
        return found, 0
    shown = [r for r in found if r["type"] != "buyback_blackout"]
    return shown, len(found) - len(shown)


def get(conn, event_id: int) -> dict | None:
    row = conn.execute(sa.select(calendar_events).where(calendar_events.c.id == event_id))
    found = row.mappings().first()
    return dict(found) if found else None


def save(conn, form: dict, now: datetime) -> int:
    """Insert (no id) or update an event from the admin form. Returns its id."""
    values = {
        "type": form["type"] if form["type"] in calendar.TYPES else "index_change",
        "market": form["market"] if form["market"] in calendar.MARKETS else "us_equity",
        "event_date": date.fromisoformat(form["event_date"]),
        "end_date": date.fromisoformat(form["end_date"]) if form.get("end_date") else None,
        "event_time": (form.get("event_time") or "").strip()[:5] or None,
        "tz": form.get("tz") if form.get("tz") in TIMEZONES else None,
        "assets": ",".join(a.strip() for a in (form.get("assets") or "").split(",") if a.strip())
        or None,
        "title": form["title"].strip(),
        "details": (form.get("details") or "").strip() or None,
        "precision": "estimated" if form.get("precision") == "estimated" else "exact",
        "major": form.get("major") == "1",
        "url": (form.get("url") or "").strip() or None,
        "edited": True,
        "updated_at": now,
    }
    if not values["event_time"]:
        values["tz"] = None
    if form.get("id"):
        event_id = int(form["id"])
        conn.execute(
            calendar_events.update().where(calendar_events.c.id == event_id).values(**values)
        )
        return event_id
    values |= {"key": f"manual|{uuid.uuid4().hex[:12]}", "source": "manual"}
    return conn.execute(calendar_events.insert().values(**values)).inserted_primary_key[0]


def delete(conn, event_id: int) -> None:
    conn.execute(calendar_events.delete().where(calendar_events.c.id == event_id))
