"""MOEX futures: open positions of individuals vs legal entities, daily (ISS, no key).

https://iss.moex.com/iss/statistics/engines/futures/markets/forts/openpositions/<ASSET>.json?date=
-> rows (tradedate, asset, is_fiz, persons_long, persons_short, open_position_long,
open_position_short, oichange_long, oichange_short), in contracts. Free and without the 14-day
delay of the FUTOI product; history goes back years (checked to 2021).

Each run asks the last `lookback_days` days (a day appears after the evening clearing; holidays
answer empty), and walks the history back `backfill_per_run` days at a time down to
`backfill_from`, so the archive fills over a few days without a separate command.
"""

from __future__ import annotations

from datetime import date, timedelta

from trade_news.collectors.base import Batch, Context, collector

URL = "https://iss.moex.com/iss/statistics/engines/futures/markets/forts/openpositions/{asset}.json"


def parse(data: dict) -> list[dict]:
    block = data.get("open_positions") or {}
    cols = block.get("columns") or []
    out = []
    for values in block.get("data") or []:
        r = dict(zip(cols, values, strict=False))
        out.append({
            "trade_date": date.fromisoformat(r["tradedate"]),
            "asset": r["asset"],
            "is_fiz": bool(r["is_fiz"]),
            "persons_long": r.get("persons_long"),
            "persons_short": r.get("persons_short"),
            "long": r.get("open_position_long"),
            "short": r.get("open_position_short"),
            "change_long": r.get("oichange_long"),
            "change_short": r.get("oichange_short"),
        })  # fmt: skip
    return out


def _day(ctx: Context, asset: str, day: date) -> list[dict]:
    resp = ctx.get(URL.format(asset=asset), params={"date": day.isoformat(), "iss.meta": "off"})
    return parse(resp.json())


@collector(
    "moex_open_positions",
    description="Мосбиржа: открытые позиции физлиц и юрлиц во фьючерсах Si, RTS, CNY",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    p = ctx.params
    today = ctx.now().date()
    assets = list(p.get("assets", ["Si", "RTS", "CNY"]))
    oldest = date.fromisoformat(str(p.get("backfill_from", "2021-01-01")))
    cur = dict(cursor or {})
    days = [today - timedelta(days=i) for i in range(int(p.get("lookback_days", 5)))]
    # history: from the day before the last walked one, backwards
    back = date.fromisoformat(cur["backfill_at"]) if cur.get("backfill_at") else days[-1]
    walk = [back - timedelta(days=i + 1) for i in range(int(p.get("backfill_per_run", 30)))]
    walk = [d for d in walk if d >= oldest]
    rows = []
    for day in days + walk:
        if day.weekday() >= 5:
            continue
        for asset in assets:
            rows += _day(ctx, asset, day)
    if walk:
        cur["backfill_at"] = walk[-1].isoformat()
    return Batch(items=[], cursor=cur, rows={"moex_open_positions": rows})
