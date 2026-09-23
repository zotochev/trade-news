"""Turning the LLM's relevance answer into an item_relevance row.

Two safety nets (spec: "главная ловушка"):
1. Window check: the resolved start must lie within [anchor - 1 day, anchor + 18 months];
   otherwise the row becomes `unknown` + needs_review instead of storing a garbage date.
2. Rule cross-check: common relative phrases ("next Thursday", "tomorrow", "в пятницу") are
   resolved from the anchor by code. If the LLM's date matches none of the acceptable
   readings, the rule's date wins (resolved_by="rule"); ambiguous phrases are flagged for review.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from trade_news.annotation.contract import Relevance

WINDOW_BEFORE = timedelta(days=1)
WINDOW_AFTER_MONTHS = 18

_WEEKDAYS = {
    # en
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
    "sunday": 6,
    # ru, in the forms that follow "в/во" and "следующий/следующую/следующее"
    "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3, "пятницу": 4,
    "пятница": 4, "субботу": 5, "суббота": 5, "воскресенье": 6,
}  # fmt: skip
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_NEXT_WD = re.compile(rf"\b(?:next|следующ\w*)\s+({_WD})\b", re.I)
_THIS_WD = re.compile(rf"\b(?:this|on|во?|this coming|coming)\s+({_WD})\b", re.I)
_DAY_WORDS = {
    "day after tomorrow": 2, "послезавтра": 2, "tomorrow": 1, "завтра": 1, "today": 0,
    "сегодня": 0, "yesterday": -1, "вчера": -1,
}  # fmt: skip
_DAY_RE = re.compile("|".join(rf"\b{re.escape(w)}\b" for w in _DAY_WORDS), re.I)
_NEXT_WEEK = re.compile(r"\b(?:next week|на следующей неделе|на будущей неделе)\b", re.I)


@dataclass(frozen=True, slots=True)
class RuleReading:
    candidates: tuple[date, ...]  # acceptable readings; first = preferred
    ambiguous: bool


def rule_dates(phrase: str | None, anchor: datetime) -> RuleReading | None:
    """Acceptable calendar dates for a relative phrase, or None if no rule applies."""
    if not phrase:
        return None
    d0 = anchor.date()
    if m := _DAY_RE.search(phrase):
        return RuleReading((d0 + timedelta(days=_DAY_WORDS[m.group(0).lower()]),), False)
    if m := _NEXT_WD.search(phrase):
        wd = _WEEKDAYS[m.group(1).lower()]
        first = _next_weekday(d0, wd)
        in_next_week = d0 + timedelta(days=7 - d0.weekday() + wd)  # that weekday of next week
        cands = tuple(dict.fromkeys([first, in_next_week]))
        return RuleReading(cands, len(cands) > 1)
    if m := _THIS_WD.search(phrase):
        wd = _WEEKDAYS[m.group(1).lower()]
        return RuleReading((_next_weekday(d0, wd, allow_today=True),), False)
    if _NEXT_WEEK.search(phrase):
        monday = d0 + timedelta(days=7 - d0.weekday())
        return RuleReading(tuple(monday + timedelta(days=i) for i in range(7)), False)
    return None


def _next_weekday(d: date, wd: int, allow_today: bool = False) -> date:
    delta = (wd - d.weekday()) % 7
    if delta == 0 and not allow_today:
        delta = 7
    return d + timedelta(days=delta)


def parse_iso(value: str | None) -> tuple[datetime, bool] | None:
    """(UTC datetime, has_time) or None. Accepts YYYY-MM-DD, YYYY-MM, full ISO datetimes."""
    if not value:
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return datetime.fromisoformat(f"{value}-01").replace(tzinfo=UTC), False
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.combine(date.fromisoformat(value), time(), UTC), False
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)), True


def _add_months(dt: datetime, months: int) -> datetime:
    y, m = divmod(dt.month - 1 + months, 12)
    day = min(dt.day, calendar.monthrange(dt.year + y, m + 1)[1])
    return dt.replace(year=dt.year + y, month=m + 1, day=day)


def _period_end(start: datetime, precision: str) -> datetime | None:
    if precision == "month":
        return _add_months(start.replace(day=1), 1) - timedelta(microseconds=1)
    if precision == "quarter":
        q_start = start.replace(month=(start.month - 1) // 3 * 3 + 1, day=1)
        return _add_months(q_start, 3) - timedelta(microseconds=1)
    return None


def in_window(dt: datetime, anchor: datetime) -> bool:
    return anchor - WINDOW_BEFORE <= dt <= _add_months(anchor, WINDOW_AFTER_MONTHS)


def resolve_relevance(rel: Relevance, anchor: datetime) -> dict:
    """item_relevance column values (without item/annotation ids)."""
    row = {
        "relevance_type": rel.type,
        "relevant_from": None,
        "relevant_to": None,
        "date_precision": rel.date_precision,
        "raw_phrase": rel.raw_phrase,
        "anchor_ts": anchor,
        "resolved_by": "llm",
        "needs_review": False,
    }
    if rel.type == "immediate":
        return row | {"relevant_from": anchor, "date_precision": "exact"}
    if rel.type == "unknown":
        return row | {"date_precision": "unknown"}

    parsed = parse_iso(rel.date_iso)
    if parsed is None:
        return row | {
            "relevance_type": "unknown",
            "date_precision": "unknown",
            "needs_review": True,
        }
    start, has_time = parsed

    if (reading := rule_dates(rel.raw_phrase, anchor)) and start.date() not in reading.candidates:
        start = datetime.combine(reading.candidates[0], time(), UTC)
        has_time = False
        row |= {"resolved_by": "rule", "needs_review": reading.ambiguous}
        if row["date_precision"] == "exact":
            row["date_precision"] = "day"

    end = None
    if rel.type == "window":
        end_parsed = parse_iso(rel.date_to_iso)
        end = end_parsed[0] if end_parsed else _period_end(start, rel.date_precision)
        if end is not None and end < start:
            end = None
    elif not has_time and row["date_precision"] == "exact":
        row["date_precision"] = "day"

    if not in_window(start, anchor) or (end is not None and not in_window(end, anchor)):
        return row | {
            "relevance_type": "unknown",
            "date_precision": "unknown",
            "resolved_by": row["resolved_by"],
            "needs_review": True,
        }
    return row | {"relevant_from": start, "relevant_to": end}
