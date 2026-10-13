"""Forced flows calendar: upcoming US Treasury auctions (TreasuryDirect, no key).

https://www.treasurydirect.gov/TA_WS/securities/upcoming?format=json lists auctions announced
for the next ~10 days: securityType (Bill / Note / Bond / TIPS / FRN), securityTerm ("10-Year",
"9-Year 10-Month" for a reopening), auctionDate, offeringAmount (empty until announced),
closingTimeCompetitive ("01:00 PM" ET). Dealers must absorb what investors don't buy, so big
coupon auctions weigh on bond prices. Bills are weekly routine and skipped unless
`include_bills`. Major: 10 years and longer, and the quarterly refunding (3/10/30-year
auctions in February, May, August, November). Key = ust_auction|CUSIP|date.
"""

from __future__ import annotations

from datetime import date, datetime

from trade_news.collectors.base import Batch, Context, collector
from trade_news.collectors.form4 import _usd
from trade_news.collectors.rss import USER_AGENT
from trade_news.flows.rules import event

URL = "https://www.treasurydirect.gov/TA_WS/securities/upcoming"
REFUNDING_MONTHS = (2, 5, 8, 11)


def _years(term: str) -> int:
    try:
        return int(term.split("-")[0])
    except ValueError:
        return 0


def _time(text: str) -> str | None:
    try:
        return datetime.strptime(text.strip(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return None


def auction_events(rows: list[dict], include_bills: bool) -> list[dict]:
    out = []
    for r in rows:
        kind, term = r.get("securityType") or "", r.get("securityTerm") or ""
        if (kind == "Bill" and not include_bills) or not r.get("auctionDate"):
            continue
        day = date.fromisoformat(r["auctionDate"][:10])
        years = _years(term) if kind != "Bill" else 0
        refunding = day.month in REFUNDING_MONTHS and years >= 3 and kind in ("Note", "Bond")
        amount = float(r["offeringAmount"]) if r.get("offeringAmount") else None
        name = f"{term} {kind}" + (" (TIPS)" if r.get("tips") == "Yes" else "")
        reopening = " (доразмещение)" if r.get("reopening") == "Yes" else ""
        out.append(event(
            "ust_auction", "us_rates", day,
            f"Аукцион Treasuries {term}{reopening}" + (f" на {_usd(amount)}" if amount else ""),
            f"Минфин США размещает {name}{reopening}"
            + (f" на {_usd(amount)}" if amount else ", объём ещё не объявлен")
            + ". Что не выкупят инвесторы, обязаны взять первичные дилеры: слабый спрос "
            "давит на цены облигаций и поднимает доходности."
            + (" Квартальный refunding." if refunding else ""),
            major=years >= 10 or refunding, time=_time(r.get("closingTimeCompetitive") or ""),
            tz="America/New_York", assets=f"UST{years}Y" if years else "T-Bills",
            size=amount, size_unit="usd" if amount else None, source="treasurydirect",
            url="https://www.treasurydirect.gov/auctions/upcoming/",
            key=f"ust_auction|{r.get('cusip')}|{day.isoformat()}",
        ))  # fmt: skip
    return out


@collector(
    "treasury_auctions",
    description="Календарь: аукционы казначейских облигаций США (TreasuryDirect)",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    rows = ctx.get(URL, params={"format": "json"}, headers={"User-Agent": USER_AGENT}).json()
    if not isinstance(rows, list):
        raise ValueError(f"unexpected TreasuryDirect response: {str(rows)[:300]}")
    now = ctx.now()
    events = auction_events(rows, bool(ctx.params.get("include_bills", False)))
    for e in events:
        e["updated_at"] = now
    return Batch(items=[], cursor=None, rows={"calendar_events": events})
