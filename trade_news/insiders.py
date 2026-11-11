"""Open-market insider trades (Form 4 codes P and S) of every role, and cluster buying.

Rows of the table insider_trades, one per transaction:
- from the sec_edgar collector: every parsed Form 4 (`rows_from_form4`), whatever passed the
  LLM thresholds (`form4_thresholds.cfo_only` only limits what is annotated);
- history from the SEC Insider Transactions Data Sets (`load_dataset`): one zip per quarter
  (https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets) with
  SUBMISSION, REPORTINGOWNER and NONDERIV_TRANS tables. Accessions already stored from EDGAR
  are skipped, so the two sources never double count.

Cluster buying (`clusters`): within 10 trading days (weekdays; exchange holidays are not
excluded) from the first buy, 2+ different insiders of one company bought on the open market,
none under a 10b5-1 plan, for `min_usd` in total. Windows don't overlap: the next one starts
with the first buy after the previous window.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa

from trade_news.db.engine import insert_ignore_many
from trade_news.db.schema import insider_trades

CODES = ("P", "S")
WINDOW_DAYS = 10  # trading days
DATASETS_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
_ROLE = {"Director": "Director", "TenPercentOwner": "10% owner", "Other": "Other"}


def _date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.strip()[:11], fmt).date()
        except ValueError:
            continue
    return None


def _float(s) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _row(accession, seq, issuer, owners, roles, planned, filed_at, url, origin, t) -> dict:
    shares, price = t["shares"], t.get("price")
    day = _date(t.get("date"))
    if (
        day
        and filed_at
        and not (
            filed_at.date() - timedelta(days=5 * 365) <= day <= filed_at.date() + timedelta(days=30)
        )
    ):
        day = None  # a typo in the filing (year 0023, 2028): no date beats a wrong one
    return {
        "accession": accession,
        "seq": seq,
        "ticker": issuer.get("ticker"),
        "issuer_cik": issuer.get("cik"),
        "issuer_name": issuer.get("name"),
        "owner": "; ".join(owners) or None,
        "roles": ", ".join(roles) or None,
        "transaction_date": day,
        "filed_at": filed_at,
        "code": t["code"],
        "shares": shares,
        "price": price,
        "usd": shares * price if price else None,
        "shares_after": t.get("shares_after"),
        "direct": t.get("direct"),
        "planned": planned,
        "url": url,
        "origin": origin,
    }


def rows_from_form4(
    accession: str, f4: dict, filed_at: datetime | None, url: str | None
) -> list[dict]:
    """`f4`: form4.Form4 as a dict (as stored in raw_items)."""
    issuer = {
        "ticker": f4.get("ticker"),
        "cik": f4.get("issuer_cik"),
        "name": f4.get("issuer_name"),
    }
    owners, roles = f4.get("owners") or [], f4.get("roles") or []
    return [
        _row(accession, i, issuer, owners, roles, f4.get("planned"), filed_at, url, "edgar", t)
        for i, t in enumerate(f4.get("trades") or [])
        if t.get("code") in CODES and t.get("shares") is not None
    ]


def backfill_from_raw(conn) -> int:
    """Rows for the Form 4 filings already in raw_items (before this table existed)."""
    from trade_news.db.schema import raw_items

    rows = []
    q = sa.select(raw_items.c.source_item_id, raw_items.c.published_at, raw_items.c.url,
                  raw_items.c.raw_json).where(raw_items.c.source == "sec_edgar")  # fmt: skip
    for acc, published, url, raw in conn.execute(q):
        if raw and raw.get("form4"):
            rows += rows_from_form4(acc, raw["form4"], published, url)
    return insert_ignore_many(conn, insider_trades, rows, "accession", "seq")


# --- SEC Insider Transactions Data Sets -------------------------------------------------


def dataset_links(html: str) -> list[str]:
    """Quarter zip paths from the data sets page, newest first (the path changed in 2026)."""
    import re

    return list(dict.fromkeys(re.findall(r'href="([^"]*/\d{4}q\d_form345\.zip)"', html)))


def _tsv(z: zipfile.ZipFile, name: str) -> Iterable[dict]:
    with z.open(name) as f:
        yield from csv.DictReader(
            io.TextIOWrapper(f, encoding="utf-8", errors="replace"),
            delimiter="\t",
            quoting=csv.QUOTE_NONE,
        )


def load_dataset(conn, data: bytes) -> int:
    """One quarter zip -> insider_trades (Form 4 and 4/A, codes P and S). Returns new rows."""
    z = zipfile.ZipFile(io.BytesIO(data))
    subs = {}
    for r in _tsv(z, "SUBMISSION.tsv"):
        if r["DOCUMENT_TYPE"] in ("4", "4/A"):
            subs[r["ACCESSION_NUMBER"]] = r
    trades = defaultdict(list)
    for r in _tsv(z, "NONDERIV_TRANS.tsv"):
        if r["ACCESSION_NUMBER"] in subs and r["TRANS_CODE"] in CODES:
            trades[r["ACCESSION_NUMBER"]].append(r)
    owners: dict[str, list[dict]] = defaultdict(list)
    for r in _tsv(z, "REPORTINGOWNER.tsv"):
        if r["ACCESSION_NUMBER"] in trades:
            owners[r["ACCESSION_NUMBER"]].append(r)
    known = set()
    accs = list(trades)
    for i in range(0, len(accs), 500):
        chunk = accs[i : i + 500]
        known |= set(conn.execute(
            sa.select(insider_trades.c.accession.distinct())
            .where(insider_trades.c.accession.in_(chunk))
        ).scalars())  # fmt: skip
    rows = []
    for acc, txs in trades.items():
        if acc in known:
            continue
        s = subs[acc]
        cik = s["ISSUERCIK"]
        names, roles = [], []
        for o in owners.get(acc, []):
            if o["RPTOWNERNAME"]:
                names.append(o["RPTOWNERNAME"])
            rel = [x.strip() for x in (o["RPTOWNER_RELATIONSHIP"] or "").split(",")]
            if o["RPTOWNER_TITLE"]:
                roles.append(o["RPTOWNER_TITLE"].strip())
            elif "Officer" in rel:
                roles.append("Officer")
            roles += [_ROLE[x] for x in rel if x in _ROLE]
        flag = (s.get("AFF10B5ONE") or "").strip()
        planned = {"1": True, "0": False}.get(flag)
        filed = _date(s["FILING_DATE"])
        filed_at = datetime(filed.year, filed.month, filed.day, tzinfo=UTC) if filed else None
        ticker = (s["ISSUERTRADINGSYMBOL"] or "").strip().upper()
        issuer = {"ticker": ticker if ticker not in ("", "NONE", "N/A") else None, "cik": cik,
                  "name": s["ISSUERNAME"] or None}  # fmt: skip
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
               f"{acc}-index.htm") if cik.isdigit() else None  # fmt: skip
        txs.sort(key=lambda r: int(r["NONDERIV_TRANS_SK"] or 0))
        for seq, t in enumerate(txs):
            shares = _float(t["TRANS_SHARES"])
            if shares is None:
                continue
            tx = {
                "code": t["TRANS_CODE"],
                "shares": shares,
                "price": _float(t["TRANS_PRICEPERSHARE"]),
                "date": t["TRANS_DATE"],
                "shares_after": _float(t["SHRS_OWND_FOLWNG_TRANS"]),
                "direct": t["DIRECT_INDIRECT_OWNERSHIP"] or None,
            }
            rows.append(_row(acc, seq, issuer, names, list(dict.fromkeys(roles)), planned,
                             filed_at, url, "sec_dataset", tx))  # fmt: skip
    return insert_ignore_many(conn, insider_trades, rows, "accession", "seq")


# --- reading ----------------------------------------------------------------------------


def trades(conn, start: date, end: date, ticker: str = "", codes=CODES, min_usd: float = 0,
           role: str = "", limit: int | None = None) -> list[dict]:  # fmt: skip
    c = insider_trades.c
    q = sa.select(insider_trades).where(
        c.transaction_date.between(start, end), c.code.in_(list(codes))
    )
    if ticker:
        q = q.where(c.ticker == ticker.upper())
    if min_usd:
        q = q.where(c.usd >= min_usd)
    if role:
        q = q.where(c.roles.ilike(f"%{role}%"))
    q = q.order_by(c.transaction_date.desc(), c.accession, c.seq)
    if limit:
        q = q.limit(limit)
    return [dict(r) for r in conn.execute(q).mappings()]


def _plus_trading_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def clusters(conn, start: date, end: date, min_usd: float = 100_000, min_insiders: int = 2,
             ticker: str = "") -> list[dict]:  # fmt: skip
    """Cluster buys that start between start and end (the window may run past `end`)."""
    c = insider_trades.c
    q = sa.select(insider_trades).where(
        c.code == "P",
        c.transaction_date.between(start, _plus_trading_days(end, WINDOW_DAYS)),
        sa.or_(c.planned.is_(None), c.planned.is_(False)),
        c.usd > 0,
    )
    if ticker:
        q = q.where(c.ticker == ticker.upper())
    by_issuer: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(q.order_by(c.transaction_date)).mappings():
        by_issuer[r["issuer_cik"] or r["ticker"] or ""].append(dict(r))
    out = []
    for key, rows in by_issuer.items():
        i = 0
        while i < len(rows):
            first = rows[i]["transaction_date"]
            last_day = _plus_trading_days(first, WINDOW_DAYS - 1)
            window = [r for r in rows[i:] if r["transaction_date"] <= last_day]
            i += len(window)
            if first > end:
                break
            people: dict[str, dict] = {}
            for r in window:
                p = people.setdefault(r["owner"] or "?", {"owner": r["owner"], "roles": r["roles"],
                                                          "usd": 0.0, "trades": 0})  # fmt: skip
                p["usd"] += r["usd"]
                p["trades"] += 1
            total = sum(p["usd"] for p in people.values())
            if len(people) < min_insiders or total < min_usd:
                continue
            top = window[-1]
            out.append({
                "key": f"{key}|{first.isoformat()}",
                "ticker": top["ticker"], "issuer_cik": top["issuer_cik"],
                "issuer_name": top["issuer_name"],
                "start": first, "end": max(r["transaction_date"] for r in window),
                "insiders": len(people), "usd": total, "trades": len(window),
                "last_filed_at": max((r["filed_at"] for r in window if r["filed_at"]),
                                     default=None),
                "owners": sorted(people.values(), key=lambda p: -p["usd"]),
            })  # fmt: skip
    out.sort(key=lambda x: (x["start"], -x["usd"]), reverse=True)
    return out


# --- cluster buys as news ----------------------------------------------------------------

SPEC_NAME = "insider_clusters"
FRESH = timedelta(days=3)  # older clusters (history, backfill) never become news


def _money(v: float) -> str:
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def cluster_importance(c: dict) -> int:
    """5 with the CEO or CFO among the buyers and $1M+ in total, else 4 (a fixed rule: the
    LLM alone gave 5 to $200K clusters)."""
    roles = " ".join(p["roles"] or "" for p in c["owners"]).upper()
    top = any(x in roles for x in ("CEO", "CFO", "CHIEF EXECUTIVE", "CHIEF FINANCIAL"))
    return 5 if top and c["usd"] >= 1_000_000 else 4


def cluster_item(c: dict):
    """A news item for a cluster (raw_items source insider_clusters)."""
    from trade_news.collectors.base import RawItem

    who = "; ".join(f"{p['owner']} ({p['roles'] or 'insider'}) {_money(p['usd'])}"
                    for p in c["owners"])  # fmt: skip
    days = (f"{c['start']:%b %d}" if c["start"] == c["end"]
            else f"{c['start']:%b %d}–{c['end']:%b %d}")  # fmt: skip
    name = c["ticker"] or c["issuer_name"] or c["issuer_cik"]
    title = (f"Cluster insider buying · {name} · {c['insiders']} insiders bought "
             f"{_money(c['usd'])} on the open market ({days}, {c['end']:%Y})")  # fmt: skip
    body = (f"{c['issuer_name']}: {c['insiders']} different insiders bought shares with their "
            f"own money within {WINDOW_DAYS} trading days, {c['trades']} transactions, "
            f"{_money(c['usd'])} in total, none under a Rule 10b5-1 plan. {who}.")  # fmt: skip
    hint = (f"cluster insider buying (Form 4, code P) in {c['ticker'] or c['issuer_name']}; "
            "several insiders buying together is a stronger signal than one buy; "
            f"set importance {cluster_importance(c)} for the company")  # fmt: skip
    facts = {k: v.isoformat() if isinstance(v, date) else v for k, v in c.items()}
    return RawItem(
        source_item_id=c["key"],
        title=title,
        body=body,
        url=None,
        published_at=c["last_filed_at"],
        raw={"hint": hint, "cluster": facts},
    )


def fresh_cluster_items(conn, now: datetime) -> list:
    found = clusters(conn, (now - timedelta(days=30)).date(), now.date())
    return [cluster_item(c) for c in found if c["last_filed_at"] and
            c["last_filed_at"] >= now - FRESH]  # fmt: skip
