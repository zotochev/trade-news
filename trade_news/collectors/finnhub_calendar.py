"""Forced flows calendar from Finnhub (free plan, key FINNHUB_API_KEY): earnings and IPO lock-ups.

- /calendar/earnings?from&to: every US company (thousands a quarter). The answer is capped at
  1500 rows and keeps the LATEST dates (checked 2026-10-13: 90 days -> only the last 2 months),
  so it is asked week by week (~13 requests per run). Only `symbols` from
  config are kept (the largest companies, whose buybacks matter for the index). For each
  report: the report itself and the estimated buyback blackout before it: companies usually
  stop buying back their own shares `blackout_days` before the report until it is out.
  Key = earnings|SYMBOL|year-Qn, so a moved date updates the row.
- /calendar/ipo?from&to: priced IPOs >= `min_ipo_usd`; the lock-up of insiders and early
  investors usually ends 180 days later (estimated: the exact term is in the prospectus).
"""

from __future__ import annotations

from datetime import date, timedelta

from trade_news.collectors.base import Batch, Context, collector
from trade_news.collectors.finnhub import BASE_URL
from trade_news.collectors.form4 import _usd
from trade_news.flows.rules import event

NY = "America/New_York"
EARNINGS_CAP = 1500
CHUNK_DAYS = 7
HOUR = {"bmo": ("до открытия", "09:00"), "amc": ("после закрытия", "16:05")}


def _get(ctx: Context, path: str, key: str, **params) -> list[dict]:
    resp = ctx.get(
        f"{BASE_URL}{path}",
        params=params,
        headers={"X-Finnhub-Token": ctx.secrets["FINNHUB_API_KEY"]},
    )
    return resp.json().get(key) or []


def earnings_events(rows: list[dict], symbols: set[str], blackout_days: int) -> list[dict]:
    out = []
    for r in rows:
        sym = r.get("symbol")
        if sym not in symbols or not r.get("date"):
            continue
        day = date.fromisoformat(r["date"])
        when, time = HOUR.get(r.get("hour") or "", ("", None))
        period = f"{r.get('year')}-Q{r.get('quarter')}"
        out.append(event(
            "earnings", "us_equity", day, f"Отчёт {sym}" + (f" {when}" if when else ""),
            f"Квартальный отчёт {sym} ({period}). После отчёта компания снова может "
            "выкупать свои акции: тихий период заканчивается.",
            assets=sym, time=time, tz=NY if time else None, source="finnhub",
            key=f"earnings|{sym}|{period}",
        ))  # fmt: skip
        out.append(event(
            "buyback_blackout", "us_equity", day - timedelta(days=blackout_days),
            f"Тихий период выкупа {sym}",
            f"Примерно за {blackout_days // 7} недели до отчёта ({day:%d.%m}) {sym} перестаёт "
            "выкупать свои акции: из рынка уходит постоянный покупатель.",
            assets=sym, end=day, precision="estimated", source="finnhub",
            key=f"buyback_blackout|{sym}|{period}",
        ))  # fmt: skip
    return out


def lockup_events(rows: list[dict], min_usd: float, lockup_days: int) -> list[dict]:
    out = []
    for r in rows:
        value = r.get("totalSharesValue") or 0
        if r.get("status") != "priced" or not r.get("symbol") or value < min_usd:
            continue
        ipo = date.fromisoformat(r["date"])
        sym = r["symbol"]
        out.append(event(
            "ipo_lockup", "us_equity", ipo + timedelta(days=lockup_days),
            f"Конец локапа {sym}",
            f"{r.get('name') or sym}: IPO {ipo:%d.%m.%Y} на {_usd(value)}. Через {lockup_days} "
            "дней инсайдеры и ранние инвесторы получают право продавать: предложение акций "
            "резко растёт. Дата — оценка, точный срок в проспекте.",
            assets=sym, precision="estimated", size=value, size_unit="usd", source="finnhub",
            major=value >= min_usd * 5, key=f"ipo_lockup|{sym}",
        ))  # fmt: skip
    return out


@collector(
    "finnhub_calendar",
    secrets=("FINNHUB_API_KEY",),
    description="Календарь: отчёты крупных компаний (тихий период выкупа) и локапы IPO (Finnhub)",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    now = ctx.now()
    today = now.date()
    p = ctx.params
    ahead = int(p.get("earnings_days", 90))
    blackout = int(p.get("blackout_days", 28))
    lockup = int(p.get("lockup_days", 180))
    rows = []
    for start in range(0, ahead + 1, CHUNK_DAYS):
        frm = today + timedelta(days=start)
        to = min(frm + timedelta(days=CHUNK_DAYS - 1), today + timedelta(days=ahead))
        chunk = _get(
            ctx,
            "/calendar/earnings",
            "earningsCalendar",
            **{"from": frm.isoformat(), "to": to.isoformat()},
        )
        if len(chunk) >= EARNINGS_CAP:
            ctx.log.warning("finnhub_earnings_capped", start=frm.isoformat(), rows=len(chunk))
        rows += chunk
    events = earnings_events(rows, set(p.get("symbols") or []), blackout)
    # IPOs whose lock-up ends from today to `ahead` days from now
    ipos = _get(
        ctx, "/calendar/ipo", "ipoCalendar",
        **{"from": (today - timedelta(days=lockup)).isoformat(),
           "to": (today + timedelta(days=ahead - lockup)).isoformat()},
    )  # fmt: skip
    events += lockup_events(ipos, float(p.get("min_ipo_usd", 100_000_000)), lockup)
    for e in events:
        e["updated_at"] = now
    return Batch(items=[], cursor=None, rows={"calendar_events": events})
