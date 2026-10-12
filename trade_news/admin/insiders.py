"""Insiders page: every parsed Form 4 with an open-market trade (P / S), whoever filed it.

Only CFO trades above the thresholds go on to the LLM and the bot (see collectors/form4.py);
the page shows the rest too, with what happened to each filing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import sqlalchemy as sa

from trade_news.collectors.form4 import _CFO_RE
from trade_news.db.schema import deliveries, items, raw_items

ROLES = {
    "cfo": ("CFO", lambda r: bool(_CFO_RE.search(r))),
    "ceo": ("CEO", lambda r: bool(re.search(r"\bCEO\b|chief executive", r, re.I))),
    "director": ("Директор (совет)", lambda r: r == "Director"),
    "owner": ("Владелец 10%", lambda r: r == "10% owner"),
}
SIDES = {"buy": "Покупки", "sell": "Продажи"}
PERIODS = {7: "7 дней", 30: "30 дней", 90: "90 дней", 3650: "Всё"}


@dataclass(frozen=True, slots=True)
class InsiderFilter:
    role: str = "cfo"  # key of ROLES or "" for anyone
    side: str = ""  # buy | sell | ""
    q: str = ""  # ticker or company
    min_usd: float = 0
    days: int = 30
    hide_planned: bool = False

    @classmethod
    def from_params(cls, p) -> InsiderFilter:
        try:
            min_usd = max(float(p.get("min_usd") or 0), 0)
        except ValueError:
            min_usd = 0
        try:
            days = int(p.get("days") or 30)
        except ValueError:
            days = 30
        return cls(
            role=p.get("role", "cfo") if p.get("role", "cfo") in ROLES else "",
            side=p.get("side", "") if p.get("side") in SIDES else "",
            q=(p.get("q") or "").strip(),
            min_usd=min_usd,
            days=days if days in PERIODS else 30,
            hide_planned=p.get("planned") == "hide",
        )


_WHY_RU = [
    ("not a CFO", "не CFO"),
    ("below thresholds", "ниже порогов"),
    ("sale under a 10b5-1 plan", "продажа по плану 10b5-1"),
    ("open-market purchase", "покупка"),
    ("open-market sale", "продажа"),
    ("buy", "покупка"),
    ("sell", "продажа"),
]


def why_ru(text: str) -> str:
    """Skip reasons from collectors/form4.py.significance in Russian."""
    for en, ru in _WHY_RU:
        text = text.replace(en, ru)
    return text


def _sum(trades: list[dict], code: str) -> tuple[float, float]:
    t = [x for x in trades if x.get("code") == code]
    return (
        sum((x.get("shares") or 0) * (x.get("price") or 0) for x in t),
        sum(x.get("shares") or 0 for x in t),
    )


def trades(conn, now: datetime, f: InsiderFilter, limit: int = 500) -> list[dict]:
    rows = conn.execute(
        sa.select(
            items.c.id,
            items.c.published_at,
            items.c.canonical_url,
            raw_items.c.raw_json,
        )
        .join(raw_items, raw_items.c.id == items.c.raw_item_id)
        .where(
            items.c.source == "sec_edgar",
            items.c.published_at >= now - timedelta(days=f.days),
        )
        .order_by(items.c.published_at.desc())
    ).all()
    sent = set(
        conn.execute(sa.select(deliveries.c.item_id).where(deliveries.c.status == "sent")).scalars()
    )
    is_role = ROLES[f.role][1] if f.role else None
    q = f.q.lower()
    out = []
    for r in rows:
        raw = r.raw_json or {}
        f4 = raw.get("form4")
        if not f4:
            continue  # 8-K, 10-Q, 13D, ...
        roles = f4.get("roles") or []
        if is_role and not any(is_role(x) for x in roles):
            continue
        if (
            q
            and q != (f4.get("ticker") or "").lower()
            and q not in (f4.get("issuer_name") or "").lower()
        ):
            continue
        if f.hide_planned and f4.get("planned"):
            continue
        tr = f4.get("trades") or []
        buy, buy_sh = _sum(tr, "P")
        sell, sell_sh = _sum(tr, "S")
        if not buy and not sell:
            continue  # grants, exercises, withholding: noise
        is_buy = buy >= sell
        if f.side and f.side != ("buy" if is_buy else "sell"):
            continue
        usd, shares = (buy, buy_sh) if is_buy else (sell, sell_sh)
        if usd < f.min_usd:
            continue
        dates = sorted(x["date"][:10] for x in tr if x.get("code") in "PS" and x.get("date"))
        skip = raw.get("llm_skip")
        out.append({
            "id": r.id,
            "filed": r.published_at,
            "traded": date.fromisoformat(dates[0]) if dates else None,
            "ticker": f4.get("ticker") or "",
            "issuer": f4.get("issuer_name") or "",
            "owners": ", ".join(f4.get("owners") or []),
            "roles": ", ".join(roles),
            "buy": is_buy,
            "usd": usd,
            "price": usd / shares if shares and usd else None,
            "planned": f4.get("planned"),
            "status": "sent" if r.id in sent else ("llm" if not skip else "skip"),
            "why": skip or raw.get("significance") or "",
            "url": r.canonical_url,
        })  # fmt: skip
        if len(out) >= limit:
            break
    return out


def totals(rows: list[dict]) -> dict:
    return {
        "n": len(rows),
        "buys": sum(1 for r in rows if r["buy"]),
        "sells": sum(1 for r in rows if not r["buy"]),
        "buy_usd": sum(r["usd"] for r in rows if r["buy"]),
        "sell_usd": sum(r["usd"] for r in rows if not r["buy"]),
        "sent": sum(1 for r in rows if r["status"] == "sent"),
    }
