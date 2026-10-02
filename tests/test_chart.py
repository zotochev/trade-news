"""Chart page: Yahoo bars (parsing, in-memory cache), news snapped to candles, the page."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.conftest import NOW
from tests.test_admin import admin, fake_bars  # noqa: F401  (fixture)
from trade_news import prices
from trade_news.admin.chart import snap


@pytest.fixture(autouse=True)
def _empty_cache():
    prices.clear_cache()
    yield
    prices.clear_cache()


def test_yahoo_symbols():
    assert prices.yahoo_symbol("equity", "BRK.B") == "BRK-B"
    assert prices.yahoo_symbol("fx", "EURUSD") == "EURUSD=X"
    assert prices.yahoo_symbol("crypto", "BTC") == "BTC-USD"
    assert prices.yahoo_symbol("crypto", "USDT") is None  # a stablecoin has no chart worth it
    assert prices.yahoo_symbol("index", "SPX") == "^GSPC"
    assert prices.yahoo_symbol("commodity", "BRENT") == "BZ=F"
    assert prices.yahoo_symbol("rates", "US2Y") is None


def test_parse_chart_drops_gaps_and_the_live_quote():
    t0 = int(datetime(2026, 10, 2, 13, 30, tzinfo=UTC).timestamp())
    data = {"chart": {"result": [{
        "timestamp": [t0, t0 + 3600, t0 + 7200, t0 + 7200 + 735],  # the last one: a live quote
        "indicators": {"quote": [{
            "open": [1, 2, 3, 4], "high": [1, 2, 3, 4], "low": [1, 2, 3, 4],
            "close": [1, None, 3, 4], "volume": [10, 20, 30, 0],
        }]},
    }]}}  # fmt: skip
    bars = prices.parse_chart(data)
    assert [b["close"] for b in bars] == [1, 3]
    assert bars[0]["ts"] == datetime(2026, 10, 2, 13, 30, tzinfo=UTC)


def test_cache_refresh_and_stale_copy_on_failure():
    calls = []

    def fetch(yahoo, interval):
        calls.append(yahoo)
        if len(calls) == 3:
            raise httpx.ConnectError("down")
        return fake_bars(NOW, 3)

    assert len(prices.get_bars("AAPL", "1h", NOW, fetch)) == 3
    prices.get_bars("AAPL", "1h", NOW + timedelta(minutes=20), fetch)  # fresh: from memory
    assert calls == ["AAPL"]
    prices.get_bars("AAPL", "1h", NOW + timedelta(minutes=40), fetch)  # older than 30 min
    assert len(calls) == 2
    stale = prices.get_bars("AAPL", "1h", NOW + timedelta(minutes=80), fetch)  # Yahoo down
    assert len(calls) == 3 and len(stale) == 3


def test_snap_news_to_the_bar_it_falls_in():
    bars = [100, 200, 300]
    news = [{"id": 1, "time": 50}, {"id": 2, "time": 200}, {"id": 3, "time": 299},
            {"id": 4, "time": 999}]  # fmt: skip
    assert [(n["id"], n["bar_time"]) for n in snap(news, bars)] == [(2, 200), (3, 200), (4, 300)]


def test_chart_page_and_data(admin):  # noqa: F811
    page = admin.get("/chart?symbol=aapl&interval=1d")
    assert page.status_code == 200 and 'state = { symbol: "AAPL", interval: "1d" }' in page.text
    assert "lightweight-charts.standalone.production.js" in page.text
    assert admin.get("/static/lightweight-charts.standalone.production.js").status_code == 200
    data = admin.get("/chart/data?symbol=AAPL&interval=1h").json()
    assert data["yahoo"] == "AAPL" and len(data["bars"]) == 12
    (news,) = data["news"]  # the Apple item, published at NOW: the 7th hourly bar
    assert news["summary"] == "Apple отчиталась лучше ожиданий" and news["direction"] == "bullish"
    assert news["bar_time"] == int(NOW.timestamp()) and news["importance"] == 4
    admin.get("/chart/data?symbol=AAPL&interval=1h")
    assert admin.calls["prices"] == [("AAPL", "1h")]  # second request served from memory
    assert admin.get("/chart/data?symbol=NOPE").status_code == 404
    assert admin.get("/chart/data?symbol=AAPL&interval=5m").status_code == 400


def test_asset_list_and_search(admin):  # noqa: F811
    page = admin.get("/chart").text
    assert '"symbol": "AAPL"' in page and '"bull": 1' in page  # the list: AAPL with a ▲ news
    assert [a["symbol"] for a in admin.get("/chart/assets?q=app").json()] == ["AAPL"]
    assert [a["symbol"] for a in admin.get("/chart/assets?q=apple").json()] == ["AAPL"]  # name
    assert admin.get("/chart/assets?q=").json() == []
