"""SEC Form 4 (insider transactions): parsing the filing and deciding whether it matters.

The feed entry only says "4 - Company (CIK) (Issuer)". The facts live in the ownership XML
inside the full submission text (`<accession>.txt`, one request per filing):
issuer ticker, who traded (director / officer title / 10% owner), and per transaction the
code, shares and price. Only open-market trades carry a signal:
- P: purchase with own money (strongest signal),
- S: sale; if flagged as a Rule 10b5-1 plan it was scheduled in advance, so no signal.
Grants (A), option exercises (M), tax withholding (F), gifts (G) etc. are noise.

Docs: https://www.sec.gov/files/forms-3-4-5.pdf (transaction codes), EDGAR ownership XML X0609.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_XML_RE = re.compile(r"<XML>\s*(.*?)\s*</XML>", re.S | re.I)
CODE_NAMES = {"P": "open-market purchase", "S": "open-market sale"}


@dataclass(frozen=True, slots=True)
class Trade:
    code: str
    shares: float
    price: float | None
    date: str | None

    @property
    def value(self) -> float | None:
        return self.shares * self.price if self.price else None


@dataclass(frozen=True, slots=True)
class Form4:
    ticker: str | None
    issuer_name: str | None
    issuer_cik: str | None
    owners: list[str]
    roles: list[str]  # "CEO", "Director", "10% owner", ...
    planned: bool | None  # Rule 10b5-1 plan flag (newer filings only)
    trades: list[Trade] = field(default_factory=list)

    def total(self, code: str) -> float:
        return sum(t.value or 0 for t in self.trades if t.code == code)

    def shares(self, code: str) -> float:
        return sum(t.shares for t in self.trades if t.code == code)


def _text(el: ET.Element | None, path: str) -> str | None:
    node = el.find(path) if el is not None else None
    if node is None:
        return None
    value = node.find("value")
    text = (value if value is not None else node).text
    return text.strip() if text and text.strip() else None


def _float(s: str | None) -> float | None:
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def _flag(s: str | None) -> bool | None:
    if s is None:
        return None
    return s.strip().lower() in ("1", "true", "y", "yes")


def parse(submission: str) -> Form4 | None:
    """Parses the full submission text (or the bare XML). None if there is no ownership XML."""
    m = _XML_RE.search(submission)
    xml = m.group(1) if m else submission
    try:
        root = ET.fromstring(xml.strip().encode())
    except ET.ParseError:
        return None
    if root.tag != "ownershipDocument":
        return None
    issuer = root.find("issuer")
    owners, roles = [], []
    for ro in root.findall("reportingOwner"):
        if name := _text(ro, "reportingOwnerId/rptOwnerName"):
            owners.append(name)
        rel = ro.find("reportingOwnerRelationship")
        if title := _text(rel, "officerTitle"):
            roles.append(title)
        elif _flag(_text(rel, "isOfficer")):
            roles.append("Officer")
        if _flag(_text(rel, "isDirector")):
            roles.append("Director")
        if _flag(_text(rel, "isTenPercentOwner")):
            roles.append("10% owner")
    trades = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        shares = _float(_text(tx, "transactionAmounts/transactionShares"))
        if not code or shares is None:
            continue
        trades.append(
            Trade(
                code=code,
                shares=shares,
                price=_float(_text(tx, "transactionAmounts/transactionPricePerShare")),
                date=_text(tx, "transactionDate"),
            )
        )
    ticker = _text(issuer, "issuerTradingSymbol")
    return Form4(
        ticker=ticker.upper() if ticker and ticker.upper() not in ("NONE", "N/A") else None,
        issuer_name=_text(issuer, "issuerName"),
        issuer_cik=_text(issuer, "issuerCik"),
        owners=owners,
        roles=list(dict.fromkeys(roles)),
        planned=_flag(_text(root, "aff10b5One")),
        trades=trades,
    )


def _usd(v: float) -> str:
    if v >= 999_500:  # rounds to $1000K otherwise
        return f"${v / 1e6:.1f}M"
    if v >= 999.5:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def describe(f: Form4) -> str:
    """One-paragraph facts for the item body (what the LLM and the admin page see)."""
    who = ", ".join(f.owners) or "insider"
    role = ", ".join(f.roles) or "insider"
    parts = []
    for code, name in CODE_NAMES.items():
        if not any(t.code == code for t in f.trades):
            continue
        total = f.total(code)
        shares = f.shares(code)
        avg = total / shares if shares and total else None
        price = f" at avg ${avg:,.2f}" if avg else " (price not reported)"
        parts.append(f"{name}: {shares:,.0f} shares{price} = {_usd(total) if total else 'n/a'}")
    other = sorted({t.code for t in f.trades} - set(CODE_NAMES))
    if other:
        parts.append(f"other transaction codes: {', '.join(other)} (grants/exercises/withholding)")
    plan = {
        True: "under a Rule 10b5-1 plan",
        False: "not under a 10b5-1 plan",
        None: "10b5-1 flag n/a",
    }[f.planned]
    return (
        f"Insider {who} ({role}) of {f.issuer_name} [{f.ticker or 'no ticker'}]: "
        + "; ".join(parts or ["no transactions"])
        + f"; {plan}."
    )


def headline(f: Form4) -> str:
    buy, sell = f.total("P"), f.total("S")
    role = (f.roles or ["insider"])[0]
    if buy >= sell and buy:
        action = f"buys {_usd(buy)}"
    elif sell:
        action = f"sells {_usd(sell)}" + (" (10b5-1 plan)" if f.planned else "")
    else:
        action = "grant/exercise, no open-market trade"
    return f"Form 4 · {f.ticker or f.issuer_name} · {role} {action}"


@dataclass(frozen=True, slots=True)
class Thresholds:
    min_buy_usd: float = 100_000
    min_sell_usd: float = 1_000_000
    skip_planned_sales: bool = True


def significance(f: Form4, th: Thresholds) -> tuple[bool, str]:
    """(worth an LLM call and possibly a signal, why)."""
    buy, sell = f.total("P"), f.total("S")
    if buy >= th.min_buy_usd:
        return True, f"open-market purchase {_usd(buy)}"
    if sell >= th.min_sell_usd:
        if f.planned and th.skip_planned_sales:
            return False, "sale under a 10b5-1 plan"
        return True, f"open-market sale {_usd(sell)}"
    if not buy and not sell:
        return False, "no open-market trades"
    return False, f"below thresholds (buy {_usd(buy)}, sell {_usd(sell)})"
