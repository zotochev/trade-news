"""Reading the forced flows calendar: calendar_events plus data we already keep elsewhere.

- token_unlocks (collector token_unlocks): one-off releases >= 1% of the unlocked supply;
  major from `MAJOR_UNLOCK_PCT`;
- central bank meetings: FOMC dates from the FedWatch snapshots (a year ahead) and rate
  decisions from the economic calendar (ForexFactory, the current week only).
Every event is a dict with the calendar_events columns (id None for the merged ones).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

import sqlalchemy as sa

from trade_news.db.schema import calendar_events, econ_events, rate_expectations, token_unlocks

MAJOR_UNLOCK_PCT = 5.0
TYPES = {
    "opex_monthly": "Экспирация опционов",
    "opex_quarterly": "Квартальная экспирация",
    "vix_expiry": "Экспирация VIX",
    "month_end": "Конец месяца",
    "quarter_end": "Конец квартала",
    "russell_recon": "Russell",
    "index_rebalance": "Ребалансировка индексов",
    "index_change": "Изменение состава индекса",
    "earnings": "Отчёт",
    "buyback_blackout": "Тихий период выкупа",
    "ipo_lockup": "Конец локапа IPO",
    "ust_auction": "Аукцион Treasuries",
    "token_unlock": "Разлок токенов",
    "cb_meeting": "Заседание ЦБ",
    "btc_halving": "Халвинг BTC",
    "ru_minfin_announce": "Анонс Минфина РФ",
    "ru_tax_period": "Налоговый период РФ",
    "ru_minfin_ops": "Операции Минфина РФ",
    "ru_dividend_cutoff": "Дивидендная отсечка",
    "ru_fx_rule": "Норма продажи валютной выручки",
}
MARKETS = {
    "us_equity": "Акции США",
    "us_rates": "Ставки США",
    "fx": "Валюты",
    "crypto": "Крипта",
    "ru": "Россия",
}
# ForexFactory titles of rate decisions -> bank
_DECISION_RE = re.compile(
    r"^(Federal Funds Rate|Main Refinancing Rate|Official Bank Rate|BOJ Policy Rate|Cash Rate|"
    r"SNB Policy Rate|Overnight Rate|Official Cash Rate)$"
)
BANKS = {"USD": "ФРС", "EUR": "ЕЦБ", "GBP": "Банк Англии", "JPY": "Банк Японии",
         "AUD": "РБА", "CHF": "ШНБ", "CAD": "Банк Канады", "NZD": "РБНЗ"}  # fmt: skip


def _base(**kw) -> dict:
    row = {c.name: None for c in calendar_events.columns}
    row.update(major=False, edited=False, precision="exact")
    row.update(kw)
    return row


def _unlocks(conn, start: date, end: date) -> list[dict]:
    at = token_unlocks.c.unlock_at
    rows = conn.execute(
        sa.select(token_unlocks).where(
            at >= datetime.combine(start, time.min, UTC), at < datetime.combine(end, time.max, UTC)
        )
    ).mappings()
    return [
        _base(
            key=f"token_unlock|{r.protocol}|{r.unlock_at:%Y-%m-%d}",
            type="token_unlock",
            market="crypto",
            event_date=r.unlock_at.date(),
            event_time=f"{r.unlock_at:%H:%M}",
            tz="UTC",
            assets=r.name,
            title=f"Разлок {r.name}: {r.pct_unlocked:.1f}% предложения",
            details=f"Разовая выдача {r.tokens:,.0f} токенов ({r.recipients}): "
            f"{r.pct_unlocked:.1f}% от уже разблокированного объёма. Получатели часто продают.",
            size=r.pct_unlocked,
            size_unit="pct_supply",
            major=r.pct_unlocked >= MAJOR_UNLOCK_PCT,
            source="defillama",
            url=f"https://defillama.com/unlocks/{r.protocol}",
        )
        for r in rows
    ]


def _cb_meetings(conn, start: date, end: date) -> list[dict]:
    out = {}
    fomc = conn.execute(
        sa.select(rate_expectations.c.meeting_date)
        .where(rate_expectations.c.meeting_date.between(start, end))
        .distinct()
    ).scalars()
    for d in fomc:
        d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
        out[("USD", d)] = _base(
            key=f"cb_meeting|{d}|USD", type="cb_meeting", market="us_rates", event_date=d,
            event_time="14:00", tz="America/New_York", assets="USD", major=True,
            title="Решение ФРС по ставке",
            details="Заседание FOMC: решение по ставке и пресс-конференция. Пересчёт позиций "
            "по всей кривой доходности и в долларе.",
            source="cme_fedwatch",
        )  # fmt: skip
    at = econ_events.c.scheduled_at
    decisions = conn.execute(
        sa.select(econ_events.c.event_name, econ_events.c.currency, at)
        .where(
            at >= datetime.combine(start, time.min, UTC), at <= datetime.combine(end, time.max, UTC)
        )
        .distinct()
    ).all()
    for name, cur, when in decisions:
        if not _DECISION_RE.match(name):
            continue
        out[(cur, when.date())] = _base(
            key=f"cb_meeting|{when:%Y-%m-%d}|{cur}", type="cb_meeting",
            market="us_rates" if cur == "USD" else "fx",
            event_date=when.date(), event_time=f"{when:%H:%M}", tz="UTC", assets=cur,
            major=True, title=f"Решение {BANKS.get(cur, cur)} по ставке",
            details=f"{name} ({cur}).", source="forexfactory",
        )  # fmt: skip
    return list(out.values())


def events(
    conn,
    start: date,
    end: date,
    types: list[str] | None = None,
    markets: list[str] | None = None,
    major_only: bool = False,
) -> list[dict]:
    """Events from start to end inclusive, by date. A window (buyback blackout) is included
    while it is open."""
    ev = calendar_events.c
    rows = [
        dict(r)
        for r in conn.execute(
            sa.select(calendar_events).where(
                sa.or_(
                    ev.event_date.between(start, end),
                    sa.and_(ev.event_date < start, ev.end_date >= start),
                )
            )
        ).mappings()
    ]
    rows += _unlocks(conn, start, end) + _cb_meetings(conn, start, end)
    rows = [
        r
        for r in rows
        if (not types or r["type"] in types)
        and (not markets or r["market"] in markets)
        and (not major_only or r["major"])
    ]
    return sorted(rows, key=lambda r: (r["event_date"], r["event_time"] or "", r["type"]))


def week_bounds(today: date) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)
