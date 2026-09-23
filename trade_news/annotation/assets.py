"""Asset reference data and resolving the LLM's free-text `symbol_or_name` to assets.id.

Reference data:
- assets.yaml: FX, crypto, indices, commodities, rates, with manual aliases (incl. Russian);
- SEC company_tickers.json: US equities (ticker, name, CIK). The first ticker of a CIK is its
  primary listing and gets the company-name aliases; other share classes get only the symbol.

Matching: normalized alias or symbol, within the asset_class the LLM gave. No match → the
caller queues it in asset_resolution_queue (never silently dropped).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml

from trade_news.db.engine import insert_ignore_many
from trade_news.db.schema import asset_aliases, assets

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ASSETS_YAML = Path(__file__).with_name("assets.yaml")

_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc",
    "llc", "lp", "sa", "ag", "nv", "se", "the", "holdings", "holding", "group", "class",
}  # fmt: skip
_NON_ALNUM = re.compile(r"[^\w]+", re.UNICODE)
# unique(alias, lang) must hold on reruns: NULLs never conflict in SQL, so language-neutral
# aliases (tickers, English names) use the BCP 47 code for "undetermined".
NEUTRAL = "und"


def norm(text: str) -> str:
    """'Apple Inc.' → 'apple', 'EUR/USD' → 'eur usd', 'Биткоин' → 'биткоин'."""
    t = unicodedata.normalize("NFKC", text).casefold().replace("&", " and ")
    words = [w for w in _NON_ALNUM.sub(" ", t).split() if w]
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    while len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def compact(text: str) -> str:
    """Symbol form: 'EUR/USD' → 'EURUSD', 'BRK.B' → 'BRKB'."""
    return _NON_ALNUM.sub("", unicodedata.normalize("NFKC", text)).upper()


def resolve(conn: sa.Connection, asset_class: str, symbol_or_name: str) -> int | None:
    """assets.id for a specific-scope link, or None."""
    sym = compact(symbol_or_name)
    by_symbol = conn.execute(
        sa.select(assets.c.id)
        .where(
            assets.c.asset_class == asset_class,
            _compact_sql(assets.c.symbol) == sym,
        )
        .order_by(assets.c.id)
        .limit(1)
    ).scalar()
    if by_symbol is not None:
        return by_symbol
    key = norm(symbol_or_name)
    if not key:
        return None
    return conn.execute(
        sa.select(asset_aliases.c.asset_id)
        .join(assets, assets.c.id == asset_aliases.c.asset_id)
        .where(
            assets.c.asset_class == asset_class, asset_aliases.c.alias_norm.in_([key, sym.lower()])
        )
        .order_by(assets.c.id)
        .limit(1)
    ).scalar()


def _compact_sql(col):
    for ch in "/.- ":
        col = sa.func.replace(col, ch, "")
    return sa.func.upper(col)


def equity_by_cik(conn: sa.Connection, cik: str) -> str | None:
    return conn.execute(
        sa.select(assets.c.symbol)
        .where(assets.c.asset_class == "equity", assets.c.cik == cik.zfill(10))
        .order_by(assets.c.id)
        .limit(1)
    ).scalar()


# --- seeding ------------------------------------------------------------------------


def yaml_assets(path: Path = ASSETS_YAML) -> list[dict[str, Any]]:
    """Flattens assets.yaml into asset dicts with an `aliases` list of (alias, lang)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = []
    for asset_class, entries in data.items():
        for e in entries:
            aliases = [(e["symbol"], NEUTRAL)]
            if e.get("name"):
                aliases.append((e["name"], NEUTRAL))
            aliases += [(a, NEUTRAL) for a in e.get("aliases", [])]
            aliases += [(a, "ru") for a in e.get("aliases_ru", [])]
            out.append(
                {
                    "asset_class": asset_class,
                    "symbol": e["symbol"],
                    "name": e.get("name"),
                    "base_ccy": e.get("base"),
                    "quote_ccy": e.get("quote"),
                    "aliases": aliases,
                }
            )
    return out


def sec_equities(data: dict[str, dict]) -> list[dict[str, Any]]:
    """company_tickers.json: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}."""
    seen_ciks: set[int] = set()
    out = []
    for row in data.values():  # file order: the first ticker of a CIK is the primary one
        cik = int(row["cik_str"])
        primary = cik not in seen_ciks
        seen_ciks.add(cik)
        aliases = [(row["ticker"], NEUTRAL)]
        if primary:
            aliases.append((row["title"], NEUTRAL))
        out.append(
            {
                "asset_class": "equity",
                "symbol": row["ticker"].upper(),
                "name": row["title"],
                "exchange": None,
                "cik": f"{cik:010d}",
                "aliases": aliases,
            }
        )
    return out


def upsert_assets(
    conn: sa.Connection, rows: Iterable[dict[str, Any]], source: str
) -> tuple[int, int]:
    """Idempotent: existing assets and aliases are kept. Returns (assets added, aliases added)."""
    rows = list(rows)
    cols = {c.name for c in assets.columns} - {"id", "is_active"}
    added = insert_ignore_many(
        conn,
        assets,
        [{k: v for k, v in r.items() if k in cols} for r in rows],
        "asset_class",
        "symbol",
    )
    # Fill columns that an earlier source left empty (e.g. assets.yaml created an equity before
    # SEC data arrived): the insert above skipped the row, so CIK/name would stay NULL forever.
    for col in ("cik", "name"):
        updates = [
            {"k_class": r["asset_class"], "k_symbol": r["symbol"], "v": r[col]}
            for r in rows
            if r.get(col)
        ]
        if updates:
            conn.execute(
                assets.update()
                .where(
                    assets.c.asset_class == sa.bindparam("k_class"),
                    assets.c.symbol == sa.bindparam("k_symbol"),
                    assets.c[col].is_(None),
                )
                .values({col: sa.bindparam("v")}),
                updates,
            )
    ids = {
        (r.asset_class, r.symbol): r.id
        for r in conn.execute(sa.select(assets.c.id, assets.c.asset_class, assets.c.symbol))
    }
    alias_rows = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        asset_id = ids[(r["asset_class"], r["symbol"])]
        for alias, lang in r["aliases"]:
            key = (alias, lang)
            if key in seen or not norm(alias):
                continue
            seen.add(key)
            alias_rows.append(
                {
                    "asset_id": asset_id,
                    "alias": alias,
                    "alias_norm": norm(alias),
                    "lang": lang,
                    "source": source,
                }
            )
    aliases_added = insert_ignore_many(conn, asset_aliases, alias_rows, "alias", "lang")
    return added, aliases_added
