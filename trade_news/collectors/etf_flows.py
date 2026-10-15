"""Daily net flows into US spot Bitcoin and Ether ETFs, million USD per fund (Farside, no key).

https://farside.co.uk/bitcoin-etf-flow-all-data/ and /ethereum-etf-flow-all-data/: one HTML
table with the whole history (BTC from 11.01.2024): a header row with the fund tickers, then a
row per trading day ("11 Jan 2024"), outflows in parentheses "(95.1)", "-" = no data yet;
service rows (Fee, Total, Average, Maximum...) are skipped. Cloudflare answers 403 / "Just a
moment" to non-browser User-Agents, so a browser one is sent (checked 2026-10-15); if it starts
blocking that too, the collector fails loudly. Fallback idea, not built: shares outstanding x
NAV from the issuers' sites.
Both pages are re-read whole each run (two requests) and upserted: Farside fills a day in the
next morning and revises late reports, so the newest numbers win.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime

from trade_news.collectors.base import Batch, Context, collector

PAGES = {
    "BTC": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    "ETH": "https://farside.co.uk/ethereum-etf-flow-all-data/",
}
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
_TICKER_RE = re.compile(r"^[A-Z0-9]{2,6}$")


def _text(cell: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())


def _value(s: str) -> float | None:
    s = s.replace(",", "").strip()
    if not s or s == "-":
        return None
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def _day(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%d %b %Y").date()
    except ValueError:
        return None


def parse(page: str, asset: str) -> list[dict]:
    if "Just a moment" in page[:5000]:
        raise ValueError("Farside answered the Cloudflare challenge page")
    table = re.search(r"<table.*?</table>", page, re.S)
    if not table:
        raise ValueError("Farside page has no table")
    rows = [
        [_text(c) for c in _CELL_RE.findall(r)]
        for r in re.findall(r"<tr.*?</tr>", table.group(0), re.S)
    ]
    # the header: the first row with fund tickers before the dates start
    funds: list[str] | None = None
    out = []
    for r in rows:
        day = _day(r[0]) if r else None
        if day is None:
            tickers = [c for c in r[1:] if _TICKER_RE.match(c)]
            if funds is None and len(tickers) >= 3:
                funds = r[1:]
            continue
        if funds is None:
            raise ValueError("Farside table header with fund tickers not found")
        for fund, cell in zip(funds, r[1:], strict=False):
            if not _TICKER_RE.match(fund):
                continue  # "Total" and empty columns
            v = _value(cell)
            if v is not None:
                out.append({"asset": asset, "trade_date": day, "fund": fund, "flow_usd_m": v})
    if not out:
        raise ValueError(f"no {asset} ETF flows parsed from Farside")
    return out


@collector(
    "etf_flows",
    description="Потоки в спотовые биткоин- и эфир-ETF США по дням и фондам (Farside)",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    now = ctx.now()
    rows = []
    for asset in ctx.params.get("assets", ["BTC", "ETH"]):
        page = ctx.get(PAGES[asset], headers={"User-Agent": BROWSER_UA}).text
        rows += [r | {"updated_at": now} for r in parse(page, asset)]
    return Batch(items=[], cursor=None, rows={"etf_flows": rows})
