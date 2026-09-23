"""How an annotated item looks in Telegram (HTML parse mode). Used by the admin's test send
now and by the delivery stage later.

    <b>AAPL ▲</b> · важность 4/5
    Apple отчиталась лучше ожиданий, выручка +8%
    Также: MSFT ▲ 3 · акции США ●
    Когда: сразу · 23.09 16:51 UTC
    Первоисточник: finnhub_market_news
"""

from __future__ import annotations

import html
from datetime import datetime

ARROW = {"bullish": "▲", "bearish": "▼", "neutral": "●"}
CLASS_RU = {
    "equity": "акции", "fx": "валюты", "crypto": "крипта", "commodity": "сырьё",
    "index": "индексы", "rates": "ставки", "macro": "макро",
}  # fmt: skip
RELEVANCE_RU = {"immediate": "сразу", "scheduled": "событие", "window": "период"}
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


def format_item(item: dict) -> str:
    """`item` as returned by admin.data.news_item: payload_json, links, relevance, raw_url…"""
    esc = html.escape
    links = sorted(
        item.get("links") or [],
        key=lambda x: (not x.get("is_primary"), -(x.get("importance") or 0)),
    )
    lines = []
    if links:
        main = links[0]
        head = f"<b>{esc(_label(main))} {ARROW.get(main.get('direction'), '')}</b>".replace(
            " </b>", "</b>"
        )
        lines.append(f"{head} · важность {main.get('importance')}/5")
    summary = (item.get("payload_json") or {}).get("summary") or item.get("title") or ""
    lines.append(esc(summary))
    others = [
        f"{esc(_label(x))} {ARROW.get(x.get('direction'), '')} {x.get('importance')}"
        for x in links[1:4]
    ]
    if others:
        lines.append("Также: " + " · ".join(others))
    if when := _when(item.get("relevance")):
        lines.append(f"Когда: {when}")
    url = item.get("raw_url") or item.get("canonical_url")
    source = esc(item.get("source") or "источник")
    if url and url.startswith(("https://", "http://")):
        lines.append(f'Первоисточник: <a href="{esc(url, quote=True)}">{source}</a>')
    else:
        lines.append(f"Источник: {source}")
    text = "\n".join(lines)
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 1] + "…"
