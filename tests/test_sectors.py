"""Sector reference: key integrity, SIC mapping on known companies, crypto and FX labels."""

import httpx
import pytest
import sqlalchemy as sa

from tests.conftest import NOW, news_spec
from trade_news import asset_sectors
from trade_news import sectors as sec
from trade_news.annotation import assets as ref
from trade_news.collectors.base import Batch, RawItem
from trade_news.db.schema import assets, item_assets, items
from trade_news.pipeline import ingest


def test_keys_unique_and_named():
    keys = sec.all_keys()
    assert len(keys) == len(set(keys))
    assert all(sec.name_ru(k) for k in keys)
    assert all(s.cycle in (None, *sec.CYCLES) for s in sec.SECTORS)


def test_every_table_key_is_known():
    for first, last, key in sec._SIC_RANGES:
        assert 100 <= first <= last <= 9999, (first, last)
        assert sec.expand(key), key
    for key in [*sec._TICKER_OVERRIDES.values(), *sec._CRYPTO.values()]:
        assert sec.expand(key), key


def test_expand():
    assert sec.expand("semiconductors") == ("technology", "semiconductors")
    assert sec.expand("technology") == ("technology", None)
    assert sec.expand("utilities") == ("utilities", None)
    assert sec.expand("tech") is None


@pytest.mark.parametrize(
    ("symbol", "sic", "expected"),
    [
        ("NVDA", 3674, ("technology", "semiconductors")),
        ("AAPL", 3571, ("technology", "hardware")),
        ("MSFT", 7372, ("technology", "software")),
        ("AMZN", 5961, ("consumer_cyclical", "retail")),
        ("TSLA", 3711, ("consumer_cyclical", "autos")),
        ("NFLX", 7841, ("communication", "media")),
        ("JPM", 6021, ("financials", "banks")),
        ("XOM", 2911, ("energy", "oil_gas")),
        ("UNH", 6324, ("healthcare", "health_services")),
        ("WMT", 5331, ("consumer_defensive", "grocery_retail")),
        ("PG", 2840, ("consumer_defensive", "household_tobacco")),
        ("LLY", 2834, ("healthcare", "pharma")),
        ("MCD", 5812, ("consumer_cyclical", "travel_leisure")),
        ("BA", 3721, ("industrials", "aerospace_defense")),
        ("KMI", 4922, ("energy", "oil_gas")),
        ("DUK", 4911, ("utilities", None)),
        ("O", "6798", ("real_estate", "reit")),
        ("NEWCO", 3827, ("technology", "hardware")),
        ("NKE", 3021, ("consumer_cyclical", "apparel_home")),
        ("IREN", 6199, ("financials", None)),
        # overrides where SIC misleads
        ("V", 7389, ("financials", "payments")),
        ("FSLR", 3674, ("energy", "renewables")),
        ("GOOGL", 7370, ("communication", "internet")),
        ("meta", 7370, ("communication", "internet")),
    ],
)
def test_classify_equity(symbol, sic, expected):
    assert sec.classify_equity(symbol, sic) == expected


@pytest.mark.parametrize("sic", [None, "", "abc", 6770, 9995, 50])
def test_classify_sic_unknown(sic):
    assert sec.classify_sic(sic) == (None, None)


def test_classify_crypto():
    assert sec.classify_crypto("btc") == ("crypto", "cryptocurrencies")
    assert sec.classify_crypto("ETH") == ("crypto", "smart_contracts")
    assert sec.classify_crypto("USDT") == ("crypto", "stablecoins")
    assert sec.classify_crypto("NEWTOKEN") == ("crypto", None)


@pytest.mark.parametrize(
    ("base", "quote", "expected"),
    [
        ("EUR", "USD", ["fx_majors"]),
        ("USD", "JPY", ["fx_majors", "fx_safe_haven"]),
        ("AUD", "JPY", ["fx_majors", "fx_commodity", "fx_safe_haven"]),
        ("USD", "TRY", ["fx_emerging"]),
        ("USD", "CNH", ["fx_emerging"]),
        (None, None, []),
    ],
)
def test_fx_industries(base, quote, expected):
    assert sec.fx_industries(base, quote) == expected


# --- filling assets -------------------------------------------------------------------

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "2": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
    "3": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    "4": {"cik_str": 9999999, "ticker": "GONE", "title": "Gone Corp"},
}
SIC_BY_CIK = {"0000320193": 3571, "0001652044": 7370, "0000789019": 7372}


class FakeSEC:
    def __init__(self, fail_after=None):
        self.calls: list[str] = []
        self.fail_after = fail_after

    def __call__(self, url, **kw):
        assert kw["headers"]["User-Agent"] == "Test test@example.com"
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise httpx.ConnectTimeout("timeout")
        cik = url.rsplit("CIK", 1)[1].removesuffix(".json")
        self.calls.append(cik)
        req = httpx.Request("GET", url)
        if cik not in SIC_BY_CIK:
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)
        return httpx.Response(200, json={"sic": str(SIC_BY_CIK[cik])}, request=req)


@pytest.fixture
def seeded(engine, cfg):
    """Equities in news: GOOGL x2, GOOG, AAPL, GONE; MSFT only in the reference."""
    with engine.begin() as conn:
        ref.upsert_assets(conn, ref.yaml_assets(), "assets.yaml")
        ref.upsert_assets(conn, ref.sec_equities(TICKERS), "sec")
        ingest(conn, news_spec(), Batch([RawItem("1", "t", "b", None, NOW, {})]), cfg, NOW)
        item_id = conn.execute(sa.select(items.c.id)).scalar()
        ids = dict(conn.execute(sa.select(assets.c.symbol, assets.c.id)).all())
        conn.execute(
            item_assets.insert(),
            [
                {"item_id": item_id, "asset_class": "equity", "scope": "specific",
                 "asset_id": ids[s]}
                for s in ("GOOGL", "GOOGL", "GOOG", "AAPL", "GONE")
            ],
        )  # fmt: skip
    return engine


def sector_of(engine, symbol):
    with engine.connect() as conn:
        return conn.execute(
            sa.select(assets.c.sector, assets.c.industry, assets.c.sic).where(
                assets.c.symbol == symbol
            )
        ).one()


def fill(engine, get, batch=100):
    return asset_sectors.fill(engine, get, "Test test@example.com", NOW, batch)


def test_fill_equities_and_reference(seeded):
    sec_api = FakeSEC()
    stats = fill(seeded, sec_api)
    assert sec_api.calls == ["0001652044", "0000320193", "0009999999"]  # one per CIK, by mentions
    assert (stats.looked_up, stats.not_found, stats.left) == (3, 1, 0)
    assert sector_of(seeded, "GOOG") == ("communication", "internet", 7370)
    assert sector_of(seeded, "AAPL") == ("technology", "hardware", 3571)
    assert sector_of(seeded, "GONE") == (None, None, None)
    assert sector_of(seeded, "MSFT") == (None, None, None)  # never in news: not looked up
    assert sector_of(seeded, "BTC")[:2] == ("crypto", "cryptocurrencies")
    assert sector_of(seeded, "USDJPY")[:2] == ("fx", "fx_majors")
    assert sector_of(seeded, "SPX")[:2] == (None, None)

    again = FakeSEC()
    stats = fill(seeded, again)
    assert again.calls == [] and stats.reference == 0  # nothing left, nothing changed


def test_fill_in_batches_and_stops_on_errors(seeded):
    stats = fill(seeded, FakeSEC(), batch=1)
    assert (stats.looked_up, stats.left) == (1, 2)
    stats = fill(seeded, FakeSEC(fail_after=1))
    assert (stats.looked_up, stats.left) == (1, 1)  # AAPL done, the SEC timed out on GONE
    assert sector_of(seeded, "AAPL")[1] == "hardware"
    with seeded.connect() as conn:
        assert asset_sectors.pending_ciks(conn) == ["0009999999"]


def test_fill_without_sec_access_does_reference_only(seeded):
    stats = asset_sectors.fill(seeded, None, None, NOW, 100)
    assert (stats.looked_up, stats.left) == (0, 3)
    assert sector_of(seeded, "ETH")[:2] == ("crypto", "smart_contracts")


def test_changed_sic_table_is_reapplied(seeded):
    fill(seeded, FakeSEC())
    with seeded.begin() as conn:
        conn.execute(assets.update().where(assets.c.symbol == "AAPL").values(industry="software"))
        assert asset_sectors.apply_reference(conn) == 1
    assert sector_of(seeded, "AAPL")[1] == "hardware"
