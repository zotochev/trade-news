"""Economic calendar: ForexFactory's weekly JSON (no key) + actual values of US data from FRED.

Feed: https://nfs.faireconomy.media/ff_calendar_thisweek.json (this week only; unofficial, may
change without notice). Event: {title, country (currency code), date (ISO with offset), impact
(High | Medium | Low | Holiday), forecast, previous}. There is NO actual value: calendars with
actuals (FMP, Finnhub) are paid (checked 2026-09-25).

1. Events with impact >= `min_impact` go to `econ_events`; a new snapshot row only when the
   forecast or previous changed (forecasts get revised).
2. US events listed in `fred_actuals` get the actual from FRED: before the release the date of
   the newest FRED observation is remembered; after the release time, once FRED has a newer
   observation, the actual is computed (level, diff, m/m or y/y %), stored as a snapshot and
   published as a news item. Gives up `release_window_hours` after the release.
   An event first seen after its release is skipped (no baseline to compare with).
High-impact USD events without a mapping are logged, so a changed title is easy to spot.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from trade_news.collectors.base import Batch, Context, RawItem, collector
from trade_news.collectors.fred import observations
from trade_news.collectors.rss import USER_AGENT

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT = {"Low": 1, "Medium": 2, "High": 3}
_VALUE_RE = re.compile(r"^[<>]?(-?\d+(?:\.\d+)?)\s*([KMBT%]?)$")
KEEP_DAYS = 14  # cursor entries of older events are dropped


def parse_value(text: str) -> tuple[float | None, str | None]:
    m = _VALUE_RE.match((text or "").strip())
    if not m:
        return None, None
    return float(m.group(1)), m.group(2) or None


def compute_actual(obs: list[tuple[date, float]], calc: str, scale: float = 1.0) -> float | None:
    """obs: ascending (date, value). None if there isn't enough history."""
    if not obs:
        return None
    last_date, last = obs[-1]
    if calc == "level":
        return last * scale
    if len(obs) < 2:
        return None
    if calc == "diff":
        return (last - obs[-2][1]) * scale
    if calc == "mom_pct":
        return (last / obs[-2][1] - 1) * 100
    if calc == "yoy_pct":
        year_ago = {d: v for d, v in obs}.get(last_date.replace(year=last_date.year - 1))
        return (last / year_ago - 1) * 100 if year_ago else None
    raise ValueError(f"unknown calc {calc!r}")


def _fmt(value: float | None, unit: str | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:g}{unit or ''}"


def release_item(key: str, e: dict, actual: float, mapping: dict) -> RawItem:
    forecast, unit = parse_value(e["forecast"])
    previous, prev_unit = parse_value(e["previous"])
    unit = unit or prev_unit
    title = (
        f"{e['country']} {e['title']}: actual {_fmt(actual, unit)} vs forecast "
        f"{_fmt(forecast, unit)}, previous {_fmt(previous, unit)}"
    )
    surprise = None if forecast is None else round(actual - forecast, 6)
    body = (
        f"Economic release {e['title']} ({e['country']}, impact {e['impact']}) at {e['date']}. "
        f"Actual {_fmt(actual, unit)}, forecast {_fmt(forecast, unit)}, previous "
        f"{_fmt(previous, unit)}"
        + (f", surprise {surprise:+g}{unit or ''}." if surprise is not None else ".")
        + f" Actual computed from FRED series {mapping['series']} ({mapping['calc']})."
    )
    return RawItem(
        source_item_id=key,
        title=title,
        body=body,
        url=f"https://fred.stlouisfed.org/series/{mapping['series']}",
        published_at=None,  # the pipeline uses fetched_at
        raw={**e, "hint": f"{e['country']} economic data release", "actual": actual},
    )


def _row(e: dict, sched: datetime, now: datetime, actual: float | None = None) -> dict:
    forecast, unit = parse_value(e["forecast"])
    previous, prev_unit = parse_value(e["previous"])
    return {
        "source": "forexfactory",
        "event_name": e["title"],
        "country": None,
        "currency": e["country"],
        "scheduled_at": sched,
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "unit": unit or prev_unit,
        "importance": IMPACT.get(e["impact"]),
        "snapshot_at": now,
    }


@collector(
    "ff_calendar",
    secrets=("FRED_API_KEY",),
    description="Экономический календарь (ForexFactory), факт по данным США из FRED",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    now = ctx.now()
    events_state: dict = {
        k: v
        for k, v in ((cursor or {}).get("events") or {}).items()
        if datetime.fromisoformat(k.rsplit("|", 1)[1]) > now - timedelta(days=KEEP_DAYS)
    }
    min_impact = IMPACT[ctx.params.get("min_impact", "Medium")]
    window = timedelta(hours=float(ctx.params.get("release_window_hours", 12)))
    mappings = {m["title"]: m for m in ctx.params.get("fred_actuals", [])}

    resp = ctx.get(ctx.params.get("url", FEED_URL), headers={"User-Agent": USER_AGENT})
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"unexpected ForexFactory response: {str(data)[:300]}")

    rows, items = [], []
    for e in data:
        if IMPACT.get(e.get("impact"), 0) < min_impact:
            continue
        key = f"{e['country']}|{e['title']}|{e['date']}"
        sched = datetime.fromisoformat(e["date"])
        st = events_state.setdefault(key, {})
        values = [e.get("forecast") or "", e.get("previous") or ""]
        if st.get("values") != values:
            rows.append(_row(e, sched, now))
            st["values"] = values

        mapping = mappings.get(e["title"]) if e["country"] == "USD" else None
        if mapping is None:
            if e["country"] == "USD" and e["impact"] == "High" and not st.get("logged"):
                ctx.log.info("ff_unmapped_usd_event", title=e["title"], date=e["date"])
                st["logged"] = True
            continue
        if st.get("done"):
            continue
        if "baseline" not in st:
            if sched <= now:  # first seen after the release: nothing to compare with
                st["done"] = True
                continue
            obs = observations(ctx, mapping["series"], now.date() - timedelta(days=62))
            st["baseline"] = obs[-1][0].isoformat() if obs else None
            continue
        if sched > now:
            continue
        obs = observations(ctx, mapping["series"], now.date() - timedelta(days=400))
        if obs and (st["baseline"] is None or obs[-1][0].isoformat() > st["baseline"]):
            actual = compute_actual(obs, mapping["calc"], float(mapping.get("scale", 1)))
            st["done"] = True
            if actual is None:
                ctx.log.warning("ff_actual_not_computed", title=e["title"], date=e["date"])
                continue
            actual = round(actual, int(mapping.get("decimals", 1)))
            rows.append(_row(e, sched, now, actual))
            items.append(release_item(key, e, actual, mapping))
        elif now - sched > window:
            ctx.log.warning("ff_actual_timeout", title=e["title"], series=mapping["series"])
            st["done"] = True

    return Batch(items=items, cursor={"events": events_state}, rows={"econ_events": rows})
