"""Forced flows calendar: computed dates (trade_news/flows/rules.py) for `horizon_days` ahead.

External data, both free and keyless:
- https://isdayoff.ru/api/getdata?year=YYYY: the Russian production calendar, one char per day
  (0 workday, 1 day off, 2 shortened workday); it has the government's day transfers that the
  `holidays` package lacks. Kept in the cursor, refreshed every `refresh_days`; on failure the
  package is used.
- https://mempool.space/api/blocks/tip/height: block height for the halving estimate.
Rows go to calendar_events by key, so a rerun updates instead of duplicating.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from trade_news.collectors.base import Batch, Context, collector
from trade_news.collectors.rss import USER_AGENT
from trade_news.flows import rules

ISDAYOFF_URL = "https://isdayoff.ru/api/getdata"
MEMPOOL_URL = "https://mempool.space/api/blocks/tip/height"


def _ru_calendar(ctx: Context, cur: dict, years: list[int], now: datetime) -> dict[str, str]:
    cached = dict(cur.get("ru_days") or {})
    fresh = cur.get("ru_days_at") and datetime.fromisoformat(cur["ru_days_at"]) > now - timedelta(
        days=float(ctx.params.get("refresh_days", 7))
    )
    if fresh and all(str(y) in cached for y in years):
        return cached
    for y in years:
        try:
            text = ctx.get(
                ISDAYOFF_URL, params={"year": y}, headers={"User-Agent": USER_AGENT}
            ).text
        except Exception as exc:
            ctx.log.warning("isdayoff_failed", year=y, error=repr(exc))
            continue
        days = 366 if date(y, 12, 31).timetuple().tm_yday == 366 else 365
        if len(text.strip()) == days and set(text.strip()) <= set("01248"):
            cached[str(y)] = text.strip()
        else:
            ctx.log.warning("isdayoff_unexpected", year=y, answer=text[:100])
    cur["ru_days"], cur["ru_days_at"] = cached, now.isoformat()
    return cached


def ru_workday(days: dict[str, str], years: list[int]):
    fallback = rules.ru_fallback_workday(years)

    def workday(d: date) -> bool:
        s = days.get(str(d.year))
        return s[d.timetuple().tm_yday - 1] in "02" if s else fallback(d)

    return workday


@collector(
    "flow_rules",
    description="Календарь вынужденных потоков: экспирации, конец месяца, индексы, Минфин РФ",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    now = ctx.now()
    cur = dict(cursor or {})
    today = now.date()
    end = today + timedelta(days=int(ctx.params.get("horizon_days", 365)))
    years = list(range(today.year, end.year + 1))
    events = rules.us_events(today, end)
    events += rules.ru_events(today, end, ru_workday(_ru_calendar(ctx, cur, years, now), years))
    try:
        height = int(ctx.get(MEMPOOL_URL, headers={"User-Agent": USER_AGENT}).text.strip())
        events.append(rules.halving(height, today))
    except Exception as exc:  # the rest of the calendar does not depend on it
        ctx.log.warning("mempool_failed", error=repr(exc))
    for e in events:
        e["updated_at"] = now
    return Batch(items=[], cursor=cur, rows={"calendar_events": events})
