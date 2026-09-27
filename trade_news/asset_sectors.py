"""Filling assets.sector / assets.industry (keys of trade_news.sectors).

- crypto and FX: from the reference by symbol / currencies, every run (cheap, no network);
- equities: SIC code from the SEC submissions API, one request per CIK, only for equities that
  appeared in news (item_assets) and were not looked up yet. The SIC is stored, so a change of
  the SIC table re-derives sector/industry of already looked-up equities without the SEC.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime

import httpx
import sqlalchemy as sa
import structlog

from trade_news import sectors
from trade_news.db.schema import assets, item_assets

log = structlog.get_logger()

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


@dataclass(slots=True)
class FillStats:
    reference: int = 0  # rows whose sector or industry changed
    looked_up: int = 0  # CIKs asked from the SEC
    not_found: int = 0
    left: int = 0  # CIKs still waiting for the next run


def _derive(row) -> tuple[str | None, str | None] | None:
    """(sector, industry) from reference data; None when the row needs an SEC lookup first."""
    if row.asset_class == "crypto":
        return sectors.classify_crypto(row.symbol)
    if row.asset_class == "fx":
        labels = sectors.fx_industries(row.base_ccy, row.quote_ccy)
        return ("fx", labels[0]) if labels else (None, None)
    if row.asset_class == "equity" and row.sector_checked_at is not None:
        return sectors.classify_equity(row.symbol, row.sic)
    return None


def apply_reference(conn: sa.Connection) -> int:
    """Sets sector/industry wherever reference data decides them. Returns rows changed."""
    rows = conn.execute(
        sa.select(
            assets.c.id, assets.c.asset_class, assets.c.symbol, assets.c.base_ccy,
            assets.c.quote_ccy, assets.c.sic, assets.c.sector, assets.c.industry,
            assets.c.sector_checked_at,
        ).where(
            sa.or_(
                assets.c.asset_class.in_(["crypto", "fx"]),
                assets.c.sector_checked_at.is_not(None),
            )
        )
    ).all()  # fmt: skip
    updates = []
    for r in rows:
        derived = _derive(r)
        if derived is not None and derived != (r.sector, r.industry):
            updates.append({"k_id": r.id, "sector": derived[0], "industry": derived[1]})
    if updates:
        conn.execute(
            assets.update()
            .where(assets.c.id == sa.bindparam("k_id"))
            .values(sector=sa.bindparam("sector"), industry=sa.bindparam("industry")),
            updates,
        )
    return len(updates)


def pending_ciks(conn: sa.Connection) -> list[str]:
    """CIKs of equities seen in news but not looked up yet, most mentioned first."""
    mentions = sa.func.count(item_assets.c.id)
    q = (
        sa.select(assets.c.cik)
        .join(item_assets, item_assets.c.asset_id == assets.c.id)
        .where(
            assets.c.asset_class == "equity",
            assets.c.cik.is_not(None),
            assets.c.sector_checked_at.is_(None),
        )
        .group_by(assets.c.cik)
        .order_by(mentions.desc(), assets.c.cik)
    )
    return list(conn.execute(q).scalars())


def fetch_sic(get: Callable[..., httpx.Response], user_agent: str, cik: str) -> int | None:
    """SIC code of a CIK; None when the SEC has no such company or no SIC for it."""
    try:
        resp = get(SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": user_agent})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    sic = resp.json().get("sic")
    try:
        return int(sic) if sic else None
    except ValueError:
        return None


def fill(
    engine: sa.Engine,
    get: Callable[..., httpx.Response] | None,
    user_agent: str | None,
    now: datetime,
    batch: int,
    write_lock=None,
) -> FillStats:
    """One pass: up to `batch` SEC lookups (skipped without get/UA), then reference data."""
    stats = FillStats()
    with engine.connect() as conn:
        ciks = pending_ciks(conn)
    stats.left = len(ciks)
    if get is not None and user_agent:
        for cik in ciks[:batch]:
            try:
                sic = fetch_sic(get, user_agent, cik)
            except httpx.HTTPError as e:
                # the SEC is down or throttling: the rest waits for the next run
                log.warning("sector_lookup_failed", cik=cik, error=type(e).__name__)
                break
            with write_lock or nullcontext(), engine.begin() as conn:
                conn.execute(
                    assets.update()
                    .where(assets.c.asset_class == "equity", assets.c.cik == cik)
                    .values(sic=sic, sector_checked_at=now)
                )
            stats.looked_up += 1
            stats.not_found += sic is None
            stats.left -= 1
    with write_lock or nullcontext(), engine.begin() as conn:
        stats.reference = apply_reference(conn)
    log.info("sectors_filled", **asdict(stats))
    return stats
