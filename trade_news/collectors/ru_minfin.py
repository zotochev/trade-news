"""Minfin FX operations under the budget rule: the monthly announcement, from news agencies.

minfin.gov.ru answers 503 "Доступ временно ограничен" to non-Russian IPs (checked 2026-10-13),
so the announcement (3rd business day, ~15:30 MSK) is read from news feeds: TASS and RIA keep
only ~1.5 hours of news, Vedomosti ~30 hours, so the collector polls every 10 minutes from the
announcement day for `window_days` days and stops once the month is found.
Typical text: "Минфин с 7 октября по 6 ноября направит на покупку валюты и золота 279,4 млрд
рублей ... ежедневный объем составит 12,7 млрд рублей". Extracted: direction (покупка /
продажа / приостановка), daily volume, total and the period; a missing daily volume is the
total divided by the business days of the period.
The Bank of Russia adds its own "mirroring" of National Wealth Fund spending, announced twice a
year: `cbr_mirroring_rub_bn_day` in config (negative = the bank sells). Net daily flow = Minfin
+ mirroring. Output: a calendar event ru_minfin_ops (the period, size = net bn RUB per day) and
one news item, delivered at once (delivery.ALWAYS_SOURCES).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from trade_news.collectors import rss
from trade_news.collectors.base import Batch, Context, RawItem, collector
from trade_news.flows import rules

FEEDS = [
    "https://tass.ru/rss/v2.xml",
    "https://ria.ru/export/rss2/archive/index.xml",
    "https://www.vedomosti.ru/rss/news",
]
_GENITIVE = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря"
MONTHS = {m: i + 1 for i, m in enumerate(_GENITIVE.split())}
_NUM = r"(\d+(?:[.,]\d+)?)"
_RELEVANT = re.compile(r"минфин", re.I)
_FX = re.compile(r"валют|юан", re.I)
_DAILY = [
    re.compile(_NUM + r"\s*млрд\s*(?:руб\w*)?\s*(?:в\s+день|ежедневно)", re.I),
    re.compile(
        r"ежедневн\w*[^.]{0,80}?(?:до|составит|составят|в\s+размере)\s*" + _NUM + r"\s*млрд", re.I
    ),
]  # fmt: skip
_TOTAL = re.compile(_NUM + r"\s*млрд\s*руб", re.I)
_PERIOD = re.compile(r"с\s+(\d{1,2})\s+([а-я]+)\s+по\s+(\d{1,2})\s+([а-я]+)", re.I)


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def _period(text: str, year: int) -> tuple[date, date] | None:
    m = _PERIOD.search(text)
    if not m or m.group(2).lower() not in MONTHS or m.group(4).lower() not in MONTHS:
        return None
    start = date(year, MONTHS[m.group(2).lower()], int(m.group(1)))
    end_month = MONTHS[m.group(4).lower()]
    end = date(year + (end_month < start.month), end_month, int(m.group(3)))
    return start, end


def extract(texts: list[str], year: int, workday) -> dict | None:
    """Operations from the matched news texts (title + description of each)."""
    texts = [t for t in texts if _RELEVANT.search(t) and _FX.search(t)]
    if not texts:
        return None
    joined = " ".join(texts)
    low = joined.lower()
    if "приостан" in low and not re.search(r"возобнов", low):
        sign = 0
    elif "продаж" in low and "покуп" not in low:
        sign = -1
    elif "покуп" in low or "приобрет" in low or "направит" in low:
        sign = 1
    else:
        return None
    daily = next((_f(m.group(1)) for rx in _DAILY for t in texts if (m := rx.search(t))), None)
    period = next((p for t in texts if (p := _period(t, year))), None)
    totals = [_f(m.group(1)) for t in texts for m in _TOTAL.finditer(t)]
    total = max((x for x in totals if x != daily), default=None)
    if daily is None and total is not None and period:
        n = sum(workday(period[0] + timedelta(days=i))
                for i in range((period[1] - period[0]).days + 1))  # fmt: skip
        daily = round(total / n, 2) if n else None
    if sign and daily is None:
        return None
    return {"sign": sign, "daily": daily or 0.0, "total": total, "period": period}


def announce_day(year: int, month: int, workday) -> date:
    d, n = date(year, month, 1), 0
    while True:
        n += workday(d)
        if n == 3:
            return d
        d += timedelta(days=1)


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def outputs(ops: dict, mirroring: float, today: date, url: str | None, now: datetime):
    sign, daily = ops["sign"], ops["daily"]
    start, end = ops["period"] or (today + timedelta(days=2), None)
    minfin = sign * daily
    net = round(minfin + mirroring, 2)
    what = {1: "покупка", -1: "продажа", 0: "операции приостановлены"}[sign]
    period = f"с {start:%d.%m}" + (f" по {end:%d.%m}" if end else "")
    title = (
        f"Минфин: {what} валюты"
        + (f" {_fmt(daily)} млрд руб в день" if sign else "")
        + f" {period}; вместе с ЦБ {'+' if net >= 0 else '−'}{_fmt(abs(net))} млрд в день"
    )
    details = (
        f"Минфин по бюджетному правилу: {what}"
        + (f" {_fmt(daily)} млрд руб в день" if sign else "")
        + (f" (всего {_fmt(ops['total'])} млрд)" if ops["total"] else "")
        + f" {period}. Зеркалирование ЦБ: {'+' if mirroring >= 0 else '−'}{_fmt(abs(mirroring))} "
        f"млрд в день. Итого {'покупки' if net >= 0 else 'продажи'} валюты {_fmt(abs(net))} млрд "
        "руб в день: "
        + ("давит на рубль." if net > 0 else "поддерживает рубль." if net < 0 else "нейтрально.")
    )  # fmt: skip
    ev = rules.event(
        "ru_minfin_ops", "ru", start, title, details, major=True, assets="USD/RUB,CNY/RUB",
        end=end, size=net, size_unit="rub_bn_day", source="news",
        precision="exact" if ops["period"] else "estimated", url=url,
        key=f"ru_minfin_ops|{today:%Y-%m}",
    )  # fmt: skip
    ev["updated_at"] = now
    item = RawItem(
        source_item_id=f"minfin|{today:%Y-%m}",
        title=title,
        body=details,
        url=url,
        published_at=None,
        raw={
            "hint": "Russian Ministry of Finance FX operations (budget rule), affects USD/RUB",
            "minfin_daily_bn": minfin,
            "mirroring_bn": mirroring,
            "net_daily_bn": net,
        },
    )
    return ev, item


@collector(
    "ru_minfin",
    description="Минфин РФ: валюта по бюджетному правилу (из лент ТАСС, РИА, Ведомостей)",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    now = ctx.now()
    today = now.date()
    cur = dict(cursor or {})
    workday = rules.ru_fallback_workday([today.year, today.year + 1])
    start = announce_day(today.year, today.month, workday)
    month = f"{today:%Y-%m}"
    window = timedelta(days=int(ctx.params.get("window_days", 3)))
    if cur.get("done") == month or not start <= today <= start + window:
        return Batch(items=[], cursor=cur)
    texts, url = [], None
    for feed in ctx.params.get("feeds", FEEDS):
        try:
            entries = rss.fetch_entries(ctx, feed)
        except Exception as exc:  # one feed down: the others may have it
            ctx.log.warning("ru_minfin_feed_failed", feed=feed, error=repr(exc))
            continue
        for e in entries:
            text = " ".join(filter(None, (e["title"], e["description"])))
            if _RELEVANT.search(text) and _FX.search(text) and re.search(r"млрд|приостан", text):
                texts.append(text)
                url = url or e["url"]
    ops = extract(texts, today.year, workday)
    if ops is None:
        return Batch(items=[], cursor=cur)
    ev, item = outputs(ops, float(ctx.params.get("cbr_mirroring_rub_bn_day", 0)), today, url, now)
    cur["done"] = month
    ctx.log.info("ru_minfin_found", daily=ops["daily"], sign=ops["sign"], period=str(ops["period"]))
    return Batch(items=[item], cursor=cur, rows={"calendar_events": [ev]})
