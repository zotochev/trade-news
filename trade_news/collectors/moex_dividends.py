"""Forced flows calendar: dividend cut-offs of the MOEX Russia index (IMOEX) shares.

MOEX ISS no longer serves dividends for free (corporate actions moved to the paid CCI product,
checked 2026-10-13), so the dates come from the smart-lab.ru dividend calendar (HTML, first
table = upcoming payments approved or recommended by the board): ticker, period, dividend RUB,
yield, "Купить До" (the last day to buy with the dividend, T+1) and the registry date.
Only IMOEX members are kept: https://iss.moex.com/iss/statistics/engines/stock/markets/index/
analytics/IMOEX.json (ticker + weight, %). The event is the last day with the dividend: the
next trading day the price gaps down by the dividend, dividend funds and arbitrageurs exit.
Major: yield >= `major_yield_pct` or index weight >= `major_weight_pct`. Key = ticker|registry.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime

from trade_news.collectors.base import Batch, Context, collector
from trade_news.collectors.rss import USER_AGENT
from trade_news.flows.rules import MSK, event

SMARTLAB_URL = "https://smart-lab.ru/dividends/"
IMOEX_URL = "https://iss.moex.com/iss/statistics/engines/stock/markets/index/analytics/IMOEX.json"
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)


def _text(cell: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())


def _num(s: str) -> float | None:
    s = s.replace("%", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_smartlab(page: str) -> list[dict]:
    """Rows of the first table (upcoming dividends), by its header names."""
    table = re.search(r"<table.*?</table>", page, re.S)
    if not table:
        return []
    rows = [
        [_text(c) for c in _CELL_RE.findall(r)]
        for r in re.findall(r"<tr.*?</tr>", table.group(0), re.S)
    ]
    if not rows or "Тикер" not in rows[0]:
        raise ValueError(f"smart-lab table header changed: {rows[:1]}")
    head = rows[0]
    out = []
    for r in rows[1:]:
        if len(r) < len(head) - 1:
            continue
        x = dict(zip(head, r, strict=False))
        buy, registry = _date(x.get("Купить До", "")), _date(x.get("Дата закрытия реестра", ""))
        if not x.get("Тикер") or not buy or not registry:
            continue
        out.append({
            "ticker": x["Тикер"], "name": x.get("Название", ""), "period": x.get("Период", ""),
            "dividend": _num(x.get("Дивиденд, руб", "")), "yield": _num(x.get("Див. Дох.", "")),
            "buy_until": buy, "registry": registry,
        })  # fmt: skip
    return out


def parse_imoex(data: dict) -> dict[str, float]:
    block = data.get("analytics") or {}
    cols = block.get("columns") or []
    return {
        r["ticker"]: float(r["weight"] or 0)
        for r in (dict(zip(cols, v, strict=False)) for v in block.get("data") or [])
    }


def dividend_events(rows: list[dict], weights: dict[str, float], today: date, p) -> list[dict]:
    out = []
    for r in rows:
        weight = weights.get(r["ticker"])
        if weight is None or r["buy_until"] < today:
            continue
        y, d = r["yield"], r["dividend"]
        major = (y or 0) >= float(p.get("major_yield_pct", 7)) or weight >= float(
            p.get("major_weight_pct", 3)
        )
        what = f"{d:g} руб" if d is not None else "дивиденд"
        out.append(event(
            "ru_dividend_cutoff", "ru", r["buy_until"],
            f"Последний день с дивидендом {r['ticker']}: {what}"
            + (f" ({y:g}%)" if y is not None else ""),
            f"{r['name']} ({r['period']}): купить до {r['buy_until']:%d.%m}, реестр "
            f"{r['registry']:%d.%m}. На следующий торговый день цена снизится на дивиденд "
            f"(гэп), дивидендные фонды и арбитражёры выходят. Вес в IMOEX {weight:g}%.",
            major=major, assets=r["ticker"], time="18:50", tz=MSK, size=y, size_unit="pct_yield",
            source="smart-lab", url=f"https://smart-lab.ru/q/{r['ticker']}/dividend/",
            key=f"ru_dividend_cutoff|{r['ticker']}|{r['registry'].isoformat()}",
        ))  # fmt: skip
    return out


@collector(
    "moex_dividends",
    description="Календарь: дивидендные отсечки бумаг индекса Мосбиржи (smart-lab + ISS)",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    now = ctx.now()
    headers = {"User-Agent": USER_AGENT}
    weights = parse_imoex(ctx.get(IMOEX_URL, params={"iss.meta": "off", "limit": 100}).json())
    if not weights:
        raise ValueError("IMOEX constituents are empty")
    rows = parse_smartlab(ctx.get(SMARTLAB_URL, headers=headers).text)
    events = dividend_events(rows, weights, now.date(), ctx.params)
    for e in events:
        e["updated_at"] = now
    return Batch(items=[], cursor=None, rows={"calendar_events": events})
