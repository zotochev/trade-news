"""FRED macro series and Treasury yields (free key).

Docs: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
Limit: 120 requests/min per key. The key is only accepted as the `api_key` query parameter,
so HTTP errors are re-raised without the URL to keep it out of the logs.

Series are listed in config.yaml. Each run asks for observations after the last stored date
(cursor per series); new ones go to `macro_observations` (first published value is kept).
Daily series with `alert_bp` (yields) create a news item when the newest day-over-day move
reaches the threshold; the first run only loads history and never alerts.
Missing values come as "." and are skipped.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from trade_news.collectors.base import Batch, Context, RawItem, collector

URL = "https://api.stlouisfed.org/fred/series/observations"


def observations(ctx: Context, series_id: str, start: date) -> list[tuple[date, float]]:
    params = {
        "series_id": series_id,
        "api_key": ctx.secrets["FRED_API_KEY"],
        "file_type": "json",
        "observation_start": start.isoformat(),
        "sort_order": "asc",
    }
    try:
        data = ctx.get(URL, params=params).json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"FRED HTTP {exc.response.status_code} for {series_id}: {exc.response.text[:300]}"
        ) from None
    out = []
    for o in data.get("observations", []):
        if o.get("value") not in (None, "", "."):
            out.append((date.fromisoformat(o["date"]), float(o["value"])))
    return out


def move_item(series: dict, prev: tuple[date, float], obs: tuple[date, float]) -> RawItem:
    (_, before), (day, after) = prev, obs
    bp = round((after - before) * 100)
    name = series.get("name", series["id"])
    title = (
        f"FRED: {name} {'rose' if bp > 0 else 'fell'} {abs(bp)} bp to {after:.2f}% "
        f"({day:%b} {day.day}, {day.year})"
    )
    return RawItem(
        source_item_id=f"fred-{series['id']}-{day.isoformat()}",
        title=title,
        body=f"{name} ({series['id']}): {before:.2f}% on {prev[0].isoformat()}, "
        f"{after:.2f}% on {day.isoformat()}, change {bp:+d} bp. Source: FRED, daily close.",
        url=f"https://fred.stlouisfed.org/series/{series['id']}",
        published_at=None,  # the pipeline uses fetched_at
        raw={"hint": series.get("hint"), "series_id": series["id"], "change_bp": bp},
    )


@collector(
    "fred",
    secrets=("FRED_API_KEY",),
    description="FRED: доходности гособлигаций США и макроданные",
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    cursor = dict(cursor or {})
    now = ctx.now()
    lookback = timedelta(days=int(ctx.params.get("lookback_days", 400)))
    items: list[RawItem] = []
    rows: list[dict] = []
    for series in ctx.params.get("series", []):
        sid = series["id"]
        last = cursor.get(sid)  # {"date": ..., "value": ...} of the newest stored observation
        start = date.fromisoformat(last["date"]) if last else now.date() - lookback
        obs = [
            o for o in observations(ctx, sid, start) if not last or o[0].isoformat() > last["date"]
        ]
        rows += [
            {"series_id": sid, "obs_date": d, "value": v, "source": "fred", "fetched_at": now}
            for d, v in obs
        ]
        # Only the newest observation can alert: after downtime older moves are stale news.
        if last and obs and (threshold := series.get("alert_bp")):
            prev = obs[-2] if len(obs) > 1 else (date.fromisoformat(last["date"]), last["value"])
            if abs(obs[-1][1] - prev[1]) * 100 >= threshold:
                items.append(move_item(series, prev, obs[-1]))
        if obs:
            cursor[sid] = {"date": obs[-1][0].isoformat(), "value": obs[-1][1]}
    return Batch(items=items, cursor=cursor, rows={"macro_observations": rows})
