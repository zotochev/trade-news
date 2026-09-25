"""How an annotated item looks in Telegram (HTML parse mode). Used by the admin's test send
and by delivery.

    📊 Макроданные · <b>USD ▲</b> · важность 4/5
    Инфляция в США за август выше прогноза
    Факт 0.4% · прогноз 0.3% · пред. 0.2% · сюрприз +0.1%
    Также: US10Y ▼ 3 · акции США ●
    Источник: Календарь + FRED

The first line tells the kind of news (by source, else by the LLM's event_type). Structured
sources add a line with their numbers from the raw data. "Когда" is shown only for scheduled
events and windows: for ordinary news it would just repeat the time of the message.
"""

from __future__ import annotations

import html
from datetime import date, datetime

from trade_news.collectors.ff_calendar import parse_value

ARROW = {"bullish": "▲", "bearish": "▼", "neutral": "●"}
CLASS_RU = {
    "equity": "акции", "fx": "валюты", "crypto": "крипта", "commodity": "сырьё",
    "index": "индексы", "rates": "ставки", "macro": "макро",
}  # fmt: skip
RELEVANCE_RU = {"scheduled": "событие", "window": "период"}
EVENT_KIND = {
    "earnings": ("📈", "Отчётность"),
    "guidance": ("🎯", "Прогноз компании"),
    "m_and_a": ("🤝", "Сделка M&A"),
    "regulatory": ("⚖️", "Регулирование"),
    "macro": ("📊", "Макро"),
    "rate_decision": ("🏦", "Решение по ставке"),
    "cb_speech": ("🎙", "Выступление ЦБ"),
    "insider": ("💼", "Инсайдер"),
}
CENTRAL_BANKS = {
    "fed_rss": "ФРС",
    "ecb_rss": "ЕЦБ",
    "boe_rss": "Банк Англии",
    "boj_rss": "Банк Японии",
}
SOURCE_RU = {
    "sec_edgar": "SEC EDGAR",
    "finnhub_market_news": "Finnhub",
    "finnhub_company_news": "Finnhub",
    "marketaux": "Marketaux",
    "coindesk": "CoinDesk",
    "cme_fedwatch": "CME FedWatch",
    "fred": "FRED",
    "ff_calendar": "Календарь + FRED",
    **CENTRAL_BANKS,
}
MAX_LEN = 4096  # Telegram message limit


def _label(link: dict) -> str:
    if link.get("symbol"):
        return link["symbol"]
    if link.get("scope") == "specific" and link.get("raw_symbol"):
        return link["raw_symbol"]
    if link.get("scope") == "group" and link.get("group_label"):
        return f"{link['group_label']} ({CLASS_RU.get(link['asset_class'], link['asset_class'])})"
    return f"{CLASS_RU.get(link['asset_class'], link['asset_class'])} в целом"


def _when(rel: dict | None) -> str | None:
    if not rel or rel.get("relevance_type") not in RELEVANCE_RU:
        return None
    start: datetime | None = rel.get("relevant_from")
    end: datetime | None = rel.get("relevant_to")
    if start is None:
        return None
    precise = rel.get("date_precision") == "exact"
    text = start.strftime("%d.%m %H:%M UTC" if precise else "%d.%m.%Y")
    if end is not None:
        text += " – " + end.strftime("%d.%m.%Y")
    return f"{RELEVANCE_RU[rel['relevance_type']]} · {text}"


def _kind(source: str, event_type: str | None, raw: dict) -> tuple[str, str]:
    if source == "ff_calendar":
        return "📊", "Макроданные"
    if source == "cme_fedwatch":
        return "🏦", "Ожидания по ставке ФРС"
    if source == "fred":
        return ("📉" if (raw.get("change_bp") or 0) < 0 else "📈"), "Доходности"
    if source == "sec_edgar" and raw.get("form4"):
        return "💼", "Инсайдер"
    if source in CENTRAL_BANKS:
        emoji, kind = EVENT_KIND.get(event_type, ("🏦", ""))
        return emoji, f"{CENTRAL_BANKS[source]}{': ' + kind.lower() if kind else ''}"
    if source == "coindesk" and event_type not in EVENT_KIND:
        return "🪙", "Крипта"
    return EVENT_KIND.get(event_type, ("📰", "Новость"))


def _num(x: float) -> str:
    return f"{x:g}"


def _rate_move_ru(outcome: str, target: str) -> str:
    """'4.00%-4.25%' vs current '3.75%-4.00%' -> 'повышение на 25 б.п.'."""
    try:
        diff = round((float(outcome.split("%")[0]) - float(target.split("%")[0])) * 100)
    except (ValueError, IndexError):
        return outcome
    if diff == 0:
        return "без изменений"
    return f"{'повышение' if diff > 0 else 'снижение'} на {abs(diff)} б.п."


def _facts(source: str, raw: dict) -> str | None:
    """A line of numbers from the source's own data, when it has any."""
    if source == "ff_calendar" and raw.get("actual") is not None:
        forecast, unit = parse_value(raw.get("forecast") or "")
        previous, prev_unit = parse_value(raw.get("previous") or "")
        unit = unit or prev_unit or ""
        parts = [f"Факт {_num(raw['actual'])}{unit}"]
        if forecast is not None:
            parts.append(f"прогноз {_num(forecast)}{unit}")
        if previous is not None:
            parts.append(f"пред. {_num(previous)}{unit}")
        if forecast is not None:
            parts.append(f"сюрприз {round(raw['actual'] - forecast, 6):+g}{unit}")
        return " · ".join(parts)
    if source == "cme_fedwatch" and raw.get("shifts"):
        target = raw.get("target") or ""
        return "\n".join(
            f"Заседание {date.fromisoformat(s['meeting']):%d.%m}: "
            f"{_rate_move_ru(s['outcome'], target)} {s['before']:.1f}% → {s['after']:.1f}%"
            for s in raw["shifts"]
        )
    if source == "fred" and raw.get("change_bp") is not None:
        return f"Изменение за день: {raw['change_bp']:+d} б.п."
    if source == "sec_edgar" and (f4 := raw.get("form4")):
        why = str(raw.get("significance") or "")
        why = why.replace("open-market purchase", "покупка на рынке").replace(
            "open-market sale", "продажа на рынке"
        )
        who = ", ".join(f4.get("roles") or []) + " " + ", ".join(f4.get("owners") or [])
        return " · ".join(p for p in (why, who.strip()) if p) or None
    return None


def format_item(item: dict) -> str:
    """`item` as returned by views.news_item: payload_json, links, relevance, raw_json, …"""
    esc = html.escape
    payload = item.get("payload_json") or {}
    raw = item.get("raw_json") or {}
    source = item.get("source") or ""
    links = sorted(
        item.get("links") or [],
        key=lambda x: (not x.get("is_primary"), -(x.get("importance") or 0)),
    )
    emoji, kind = _kind(source, payload.get("event_type"), raw)
    head = f"{emoji} {esc(kind)}"
    if links:
        main = links[0]
        asset = f"{_label(main)} {ARROW.get(main.get('direction'), '')}".strip()
        head += f" · <b>{esc(asset)}</b> · важность {main.get('importance')}/5"
    lines = [head, esc(payload.get("summary") or item.get("title") or "")]
    if facts := _facts(source, raw):
        lines.append(esc(facts))
    others = [
        f"{esc(_label(x))} {ARROW.get(x.get('direction'), '')} {x.get('importance')}"
        for x in links[1:4]
    ]
    if others:
        lines.append("Также: " + " · ".join(others))
    if when := _when(item.get("relevance")):
        lines.append(f"Когда: {when}")
    url = item.get("raw_url") or item.get("canonical_url")
    name = esc(SOURCE_RU.get(source, source or "источник"))
    if url and url.startswith(("https://", "http://")):
        lines.append(f'Источник: <a href="{esc(url, quote=True)}">{name}</a>')
    else:
        lines.append(f"Источник: {name}")
    text = "\n".join(lines)
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 1] + "…"
