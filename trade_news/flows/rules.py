"""Calendar rules: forced-flow dates computed without external data.

US dates follow the NYSE holiday calendar (`holidays` package). Russian ones take a
`ru_workday(date) -> bool` callable: the official production calendar (with the government's
day transfers) comes from isdayoff.ru in the collector, with a fallback to the package.

Sources of the rules (checked 2026-10-13):
- monthly options expire on the 3rd Friday; on an exchange holiday, the day before (Cboe);
- VIX: the Wednesday 30 days before the 3rd Friday of the next month; if that Friday is a
  holiday, 30 days before the business day preceding it; a holiday Wednesday -> the day before;
- Russell US indexes: semi-annual from 2026, effective after the close on the 4th Friday of
  June and the 2nd Friday of December (FTSE Russell, 2026 schedule: 26.06 and 11.12);
- MSCI quarterly reviews: effective after the close on the last business day of Feb/May/Aug/Nov;
- S&P 500 / Nasdaq-100 quarterly rebalancing: after the close on the 3rd Friday of
  Mar/Jun/Sep/Dec (Nasdaq-100 reconstitution is the December one);
- Minfin announces the month's FX operations on the 3rd business day (budget rule);
- the single tax payment is due on the 28th (next business day if it is a day off);
- Bitcoin halving every 210,000 blocks, ~10 minutes per block.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import date, timedelta

import holidays

NY = "America/New_York"
MSK = "Europe/Moscow"
HALVING_BLOCKS = 210_000
BLOCK_MINUTES = 10


def event(
    type_: str,
    market: str,
    day: date,
    title: str,
    details: str,
    *,
    major: bool = False,
    precision: str = "exact",
    assets: str | None = None,
    time: str | None = None,
    tz: str | None = None,
    end: date | None = None,
    size: float | None = None,
    size_unit: str | None = None,
    source: str = "rules",
    url: str | None = None,
    key: str | None = None,
) -> dict:
    """One calendar_events row (without timestamps). Key: type|date[|assets]."""
    return {
        "key": key or "|".join(filter(None, (type_, day.isoformat(), assets))),
        "type": type_,
        "market": market,
        "event_date": day,
        "end_date": end,
        "event_time": time,
        "tz": tz,
        "assets": assets,
        "title": title,
        "details": details,
        "precision": precision,
        "size": size,
        "size_unit": size_unit,
        "major": major,
        "source": source,
        "url": url,
    }


class Nyse:
    def __init__(self, years: list[int]):
        self.holidays = holidays.financial_holidays("NYSE", years=years)

    def open(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def before(self, d: date) -> date:
        """The trading day on or before d."""
        while not self.open(d):
            d -= timedelta(days=1)
        return d


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def third_friday(year: int, month: int) -> date:
    return nth_weekday(year, month, calendar.FRIDAY, 3)


def last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _months(start: date, end: date):
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def us_events(start: date, end: date) -> list[dict]:
    nyse = Nyse(list(range(start.year, end.year + 2)))
    out = []
    for y, m in _months(start, end):
        quarter = m in (3, 6, 9, 12)
        opex = nyse.before(third_friday(y, m))
        if quarter:
            extra = " и Nasdaq-100 (декабрь — ежегодный пересмотр состава)" if m == 12 else ""
            out.append(event(
                "opex_quarterly", "us_equity", opex,
                "Квартальная экспирация опционов («тройная ведьма»)",
                "Истекают квартальные опционы и фьючерсы на индексы: маркетмейкеры снимают "
                "хеджи, огромные объёмы на закрытии. В этот же день ребалансировка S&P 500"
                f"{extra}: индексные фонды покупают и продают по цене закрытия.",
                major=True, time="16:00", tz=NY, assets="SPX",
            ))  # fmt: skip
        else:
            out.append(event(
                "opex_monthly", "us_equity", opex, "Месячная экспирация опционов (США)",
                "Истекают месячные опционы: маркетмейкеры снимают дельта-хеджи, после "
                "экспирации «пришпиливание» цены к крупным страйкам ослабевает.",
                time="16:00", tz=NY, assets="SPX",
            ))  # fmt: skip

        # VIX: 30 days before the next month's 3rd Friday (or the trading day before it)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        vix = nyse.before(nyse.before(third_friday(ny, nm)) - timedelta(days=30))
        out.append(event(
            "vix_expiry", "us_equity", vix, "Экспирация VIX",
            "Расчёт фьючерсов и опционов на VIX по утренним котировкам опционов S&P 500: "
            "закрытие хеджей волатильности.",
            time="09:30", tz=NY, assets="VIX",
        ))  # fmt: skip

        last = nyse.before(last_day(y, m))
        if quarter:
            out.append(event(
                "quarter_end", "us_equity", last,
                "Конец года" if m == 12 else "Конец квартала",
                "Ребалансировка пенсионных фондов между акциями и облигациями, хедж валютных "
                "позиций иностранцами, «причёсывание» отчётных портфелей, спрос на доллар.",
                major=True, time="16:00", tz=NY,
            ))  # fmt: skip
        else:
            out.append(event(
                "month_end", "us_equity", last, "Конец месяца",
                "Ребалансировка фондов со смешанным мандатом, хедж валютной переоценки.",
                time="16:00", tz=NY,
            ))  # fmt: skip

        if m in (2, 5, 8, 11):
            out.append(event(
                "index_rebalance", "us_equity", last, "Пересмотр индексов MSCI",
                "Квартальный пересмотр MSCI вступает в силу после закрытия: пассивные фонды "
                "покупают добавленные и продают исключённые бумаги по цене закрытия.",
                major=True, time="16:00", tz=NY, assets="MSCI",
            ))  # fmt: skip
        if m in (6, 12):
            friday = nth_weekday(y, m, calendar.FRIDAY, 4 if m == 6 else 2)
            out.append(event(
                "russell_recon", "us_equity", nyse.before(friday),
                "Реконституция индексов Russell",
                "Индексы Russell 1000/2000 пересобираются после закрытия: фонды малых компаний "
                "одним днём перекладывают десятки миллиардов, рекордный объём на закрытии.",
                major=True, time="16:00", tz=NY, assets="RUT",
                precision="exact" if y <= 2026 else "estimated",
                url="https://www.lseg.com/en/ftse-russell/russell-reconstitution",
            ))  # fmt: skip
    return [e for e in out if start <= e["event_date"] <= end]


def ru_fallback_workday(years: list[int]) -> Callable[[date], bool]:
    """Weekends and holidays from the package; misses the government's day transfers."""
    ru = holidays.RU(years=years)
    return lambda d: d.weekday() < 5 and d not in ru


def ru_events(start: date, end: date, workday: Callable[[date], bool]) -> list[dict]:
    out = []
    for y, m in _months(start, end):
        d, n = date(y, m, 1), 0
        while True:
            n += workday(d)
            if n == 3:
                break
            d += timedelta(days=1)
        out.append(event(
            "ru_minfin_announce", "ru", d, "Минфин: операции с валютой на месяц",
            "Минфин объявляет объём покупок или продаж валюты по бюджетному правилу на месяц "
            "(с учётом «зеркалирования» ЦБ): задаёт ежедневный поток в рубле на месяц вперёд.",
            major=True, time="15:30", tz=MSK, assets="USD/RUB",
            url="https://minfin.gov.ru/ru/press-center/",
        ))  # fmt: skip
        tax = date(y, m, 28)
        while not workday(tax):
            tax += timedelta(days=1)
        out.append(event(
            "ru_tax_period", "ru", tax, "Налоговый период (ЕНП)",
            "Срок единого налогового платежа. За неделю до него экспортёры продают валюту, "
            "чтобы заплатить налоги в рублях: рубль обычно крепнет.",
            assets="USD/RUB", end=tax, precision="exact",
        ))  # fmt: skip
    return [e for e in out if start <= e["event_date"] <= end]


def halving(height: int, today: date) -> dict:
    target = (height // HALVING_BLOCKS + 1) * HALVING_BLOCKS
    left = target - height
    when = today + timedelta(minutes=left * BLOCK_MINUTES)
    return event(
        "btc_halving", "crypto", when, f"Халвинг биткоина (блок {target:,})".replace(",", " "),
        f"Награда майнерам за блок уменьшится вдвое: до блока {left:,} ".replace(",", " ")
        + "блоков, дата — оценка по 10 минут на блок. Майнеры с высокой себестоимостью "
        "вынуждены продавать BTC или отключаться.",
        major=True, precision="estimated", assets="BTC", size=target, size_unit="block",
        key=f"btc_halving|{target}", url="https://mempool.space",
    )  # fmt: skip
