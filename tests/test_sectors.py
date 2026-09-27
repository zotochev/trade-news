"""Sector reference: key integrity, SIC mapping on known companies, crypto and FX labels."""

import pytest

from trade_news import sectors as sec


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
