"""Telegram: the Monday digest of the week's forced flows and reminders on the eve of major ones.

- digest (flows.digest_weekday / digest_hour_utc): the week Monday..Sunday grouped by day, one
  line per event "what — who is forced and which way"; major events in bold. Many similar
  events are folded: earnings of a day become one line, buyback blackouts one line for the
  week (how many of the tracked companies are in the window).
- reminders (flows.reminder_hour_utc, every day): major events of the next business day (on
  Friday: Saturday to Monday). Keys already reminded are kept in `settings`, so a restart does
  not repeat them.
Both are sent only when the delivery is enabled (same switch as the news).
The delivery rules also pick the markets shown (`calendar_markets`, `calendar_banks`).
"""

from __future__ import annotations

import html
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta

import sqlalchemy as sa
import structlog

from trade_news import delivery
from trade_news.db.schema import settings
from trade_news.flows import calendar
from trade_news.http import RateLimiter
from trade_news.telegram import subscribers as subs
from trade_news.telegram.api import Api, TelegramError
from trade_news.telegram.message import MAX_LEN

log = structlog.get_logger()

REMINDED_KEY = "flows_reminded"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
# one line "who is forced and which way" per type (details are longer, for the admin and API)
WHO = {
    "opex_monthly": "маркетмейкеры снимают хеджи",
    "opex_quarterly": "снятие хеджей + ребалансировка S&P 500, объёмы на закрытии",
    "vix_expiry": "закрытие хеджей волатильности",
    "month_end": "ребалансировка фондов",
    "quarter_end": "пенсионные фонды ребалансируют акции/облигации, спрос на доллар",
    "russell_recon": "фонды малых компаний перекладывают портфели на закрытии",
    "index_rebalance": "пассивные фонды покупают добавленные, продают исключённые",
    "index_change": "индексные фонды обязаны купить/продать",
    "ipo_lockup": "инсайдеры получают право продавать",
    "ust_auction": "дилеры обязаны взять невыкупленное",
    "token_unlock": "получатели часто продают",
    "cb_meeting": "пересчёт позиций по ставкам",
    "btc_halving": "майнеры с высокой себестоимостью продают",
    "ru_minfin_announce": "задаёт ежедневные покупки/продажи валюты на месяц",
    "ru_tax_period": "экспортёры продают валюту, рубль крепнет",
    "ru_dividend_cutoff": "после отсечки цена снизится на дивиденд",
    "ru_fx_rule": "экспортёры обязаны продавать больше или меньше валюты",
}


def _when(e: dict) -> str:
    if not e["event_time"]:
        return ""
    tz = {"America/New_York": "NY", "Europe/Moscow": "МСК", "UTC": "UTC"}.get(e["tz"], "")
    return f"{e['event_time']} {tz} · ".replace("  ", " ")


def _line(e: dict) -> str:
    who = WHO.get(e["type"])
    text = html.escape(e["title"]) + (f" — {html.escape(who)}" if who else "")
    if e["precision"] == "estimated":
        text += " <i>(оценка)</i>"
    title = f"<b>{text}</b>" if e["major"] else text
    return f"• {_when(e)}{title}"


def format_week(events: list[dict], start: date, end: date) -> list[str]:
    head = f"🗓 <b>Вынужденные потоки: {start:%d.%m} – {end:%d.%m}</b>\n"
    blackout = sorted({e["assets"] for e in events if e["type"] == "buyback_blackout"})
    lines = []
    if blackout:
        lines.append(
            f"Тихий период выкупа у {len(blackout)} крупных компаний: из рынка уходит "
            f"покупатель собственных акций ({', '.join(blackout[:12])}"
            + (" …" if len(blackout) > 12 else "")
            + ").\n"
        )
    by_day: dict[date, list[dict]] = defaultdict(list)
    for e in events:
        if e["type"] != "buyback_blackout" and start <= e["event_date"] <= end:
            by_day[e["event_date"]].append(e)
    for day in sorted(by_day):
        lines.append(f"<b>{WEEKDAYS[day.weekday()]} {day:%d.%m}</b>")
        reports = [e for e in by_day[day] if e["type"] == "earnings"]
        for e in by_day[day]:
            if e["type"] != "earnings":
                lines.append(_line(e))
        if reports:
            tickers = ", ".join(html.escape(e["assets"] or "") for e in reports)
            lines.append(f"• Отчёты: {tickers} — после отчёта возобновляется выкуп акций")
        lines.append("")
    if not by_day and not blackout:
        lines.append("На этой неделе крупных вынужденных потоков не ожидается.")
    return _split(head, lines)


def format_reminder(events: list[dict]) -> list[str]:
    head = "⏰ <b>Завтра: вынужденные потоки</b>\n"
    lines = []
    for e in events:
        day = f"{WEEKDAYS[e['event_date'].weekday()]} {e['event_date']:%d.%m}"
        lines.append(f"{_line(e)} ({day})")
        if e["details"]:
            lines.append(f"  <i>{html.escape(e['details'])}</i>")
    return _split(head, lines)


def _split(head: str, lines: list[str]) -> list[str]:
    out, cur = [], head
    for line in lines:
        if len(cur) + len(line) + 1 > MAX_LEN:
            out.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    out.append(cur.rstrip())
    return out


def build_digest(engine: sa.Engine, today: date) -> list[str]:
    start, end = calendar.week_bounds(today)
    with engine.connect() as conn:
        rules = delivery.load_rules(conn)
        events = calendar.events(conn, start, end)
    return format_week([e for e in events if delivery.calendar_wanted(rules, e)], start, end)


def due_reminders(engine: sa.Engine, today: date) -> list[dict]:
    """Major events of the next business day (Friday: up to Monday), not reminded yet."""
    last = today + timedelta(days=1)
    while last.weekday() >= 5:
        last += timedelta(days=1)
    with engine.connect() as conn:
        rules = delivery.load_rules(conn)
        events = calendar.events(conn, today + timedelta(days=1), last, major_only=True)
        done = _reminded(conn)
    # a window (blackout) is only reminded when it starts
    return [
        e
        for e in events
        if e["key"] not in done and e["event_date"] > today and delivery.calendar_wanted(rules, e)
    ]


def _reminded(conn) -> dict:
    value = conn.execute(sa.select(settings.c.value).where(settings.c.key == REMINDED_KEY)).scalar()
    return dict(value or {})


def _mark_reminded(engine: sa.Engine, keys: list[str], today: date, now: datetime) -> None:
    with engine.begin() as conn:
        done = {
            k: v
            for k, v in _reminded(conn).items()
            if v >= (today - timedelta(days=30)).isoformat()
        }
        done |= {k: today.isoformat() for k in keys}
        updated = conn.execute(
            settings.update()
            .where(settings.c.key == REMINDED_KEY)
            .values(value=done, updated_at=now)
        ).rowcount
        if not updated:
            conn.execute(settings.insert().values(key=REMINDED_KEY, value=done, updated_at=now))


def send(
    engine: sa.Engine, api: Api, root_chat_id: int | None, messages: list[str], now: datetime
) -> int:
    """To every recipient, when the delivery is enabled. Returns messages sent."""
    with engine.connect() as conn:
        if not delivery.load_rules(conn).enabled:
            return 0
        recipients = subs.recipients(conn, root_chat_id)
    sent = 0
    per_chat = RateLimiter(calls=1, period=1.1)
    for chat_id in recipients if messages else []:
        for text in messages:
            per_chat.acquire()
            try:
                api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                    disable_web_page_preview=True)  # fmt: skip
                sent += 1
            except TelegramError as exc:
                if exc.chat_unreachable and chat_id != root_chat_id:
                    with engine.begin() as conn:
                        subs.unsubscribe(conn, chat_id, "blocked", now)
                log.warning("flows_send_failed", chat_id=chat_id, error=str(exc))
                break
    return sent


def run_digest(
    engine: sa.Engine, api: Api, root_chat_id: int | None, now: Callable[[], datetime]
) -> int:
    ts = now()
    sent = send(engine, api, root_chat_id, build_digest(engine, ts.date()), ts)
    log.info("flows_digest_done", sent=sent)
    return sent


def run_reminders(
    engine: sa.Engine, api: Api, root_chat_id: int | None, now: Callable[[], datetime]
) -> int:
    ts = now()
    events = due_reminders(engine, ts.date())
    if not events:
        return 0
    sent = send(engine, api, root_chat_id, format_reminder(events), ts)
    _mark_reminded(engine, [e["key"] for e in events], ts.date(), ts)
    log.info("flows_reminders_done", events=len(events), sent=sent)
    return sent
