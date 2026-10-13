import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from tests.conftest import NOW, fake_ctx, news_spec
from tests.test_admin import admin  # noqa: F401  (the fixture: admin app over `engine`)
from trade_news.collectors import crypto_leverage as cl
from trade_news.crypto import history
from trade_news.crypto import liquidations as liq
from trade_news.db.schema import crypto_derivs, crypto_liquidations
from trade_news.pipeline import ingest

MS = int(NOW.timestamp() * 1000)


def test_rank_keeps_majors_and_skips_excluded():
    tickers = [
        {"symbol": "XAUTUSDT", "openInterestValue": "9e9"},
        {"symbol": "SOLUSDT", "openInterestValue": "5e8"},
        {"symbol": "ETHUSDT", "openInterestValue": "2e9"},
        {"symbol": "DOGEUSDT", "openInterestValue": "3e8"},
        {"symbol": "BTCPERP", "openInterestValue": "9e10"},  # not a USDT perpetual
    ]
    assert cl.rank(tickers, ["BTC", "ETH"], 1, {"XAUT"}) == ["BTC", "ETH", "SOL"]


def test_snapshot_grid():
    assert cl.snapshot_ts(datetime(2026, 10, 13, 12, 7, 31, tzinfo=UTC)) == datetime(
        2026, 10, 13, 12, 5, tzinfo=UTC
    )


def _exchange_get(url, params=None, **kw):
    def ok(data):
        return SimpleNamespace(json=lambda: data)

    p = params or {}
    if url.endswith("/v5/market/tickers"):
        return ok({"result": {"list": [
            {"symbol": "BTCUSDT", "openInterestValue": "4.0e9", "openInterest": "50000",
             "fundingRate": "0.0001", "fundingIntervalHour": "8", "markPrice": "80000"},
            {"symbol": "ETHUSDT", "openInterestValue": "2.0e9", "openInterest": "800000",
             "fundingRate": "-0.0002", "fundingIntervalHour": "4", "markPrice": "2500"},
        ]}})  # fmt: skip
    if url.endswith("/account-ratio"):
        return ok({"result": {"list": [{"buyRatio": "0.6", "sellRatio": "0.4"}]}})
    if url.endswith("/premiumIndex"):
        return ok([{"symbol": "BTCUSDT", "markPrice": "80010", "lastFundingRate": "0.00005"}])
    if url.endswith("/fundingInfo"):
        return ok([])
    if url.endswith("/fapi/v1/openInterest"):
        return ok({"openInterest": "100000"})
    if url.endswith("globalLongShortAccountRatio"):
        return ok([{"longShortRatio": "1.25"}])
    if url.endswith("/public/open-interest"):
        return ok({"data": [
            {"instId": "BTC-USDT-SWAP", "oiUsd": "3e9", "oiCcy": "37500"},
            {"instId": "BTC-USD-SWAP", "oiUsd": "1e9", "oiCcy": "12500"},
            {"instId": "BTC-USDC-SWAP", "oiUsd": "5e8", "oiCcy": "6250"},
        ]})  # fmt: skip
    if url.endswith("/public/funding-rate"):
        assert p["instId"] == "BTC-USDT-SWAP"
        return ok({"data": [{"fundingRate": "0.00003", "fundingTime": "0",
                             "nextFundingTime": str(8 * 3_600_000)}]})  # fmt: skip
    if "long-short-account-ratio" in url:
        return ok({"data": [["1", "1.1"]]})
    raise AssertionError(url)


def test_collector_snapshot_from_three_exchanges(engine, cfg):
    ctx = fake_ctx(get=_exchange_get, params={"always": ["BTC", "ETH"], "top": 0})
    batch = cl.fetch(ctx, None)
    rows = {(r["exchange"], r["symbol"]): r for r in batch.rows["crypto_derivs"]}
    assert set(rows) == {("bybit", "BTC"), ("bybit", "ETH"), ("binance", "BTC"), ("okx", "BTC")}
    assert abs(rows[("bybit", "BTC")]["long_short_ratio"] - 1.5) < 1e-9
    assert rows[("bybit", "ETH")]["funding_interval_h"] == 4
    assert rows[("binance", "BTC")]["oi_usd"] == 100000 * 80010
    assert rows[("okx", "BTC")]["oi_usd"] == 4e9  # USDT + coin-margined, not USDC
    assert rows[("okx", "BTC")]["funding_interval_h"] == 8
    assert batch.cursor["symbols"] == ["BTC", "ETH"]
    spec = news_spec("crypto_leverage", title_dedup=False)
    with engine.begin() as conn:
        assert ingest(conn, spec, batch, cfg, NOW).rows == 4
        assert ingest(conn, spec, batch, cfg, NOW).rows == 0


def test_one_exchange_down_keeps_the_others():
    def get(url, **kw):
        if "binance" in url:
            raise ConnectionError("down")
        return _exchange_get(url, **kw)

    batch = cl.fetch(fake_ctx(get=get, params={"top": 0}), None)
    assert {r["exchange"] for r in batch.rows["crypto_derivs"]} == {"bybit", "okx"}


def test_parse_liquidation_messages():
    b = liq.parse_binance({"E": MS, "o": {"s": "BTCUSDT", "S": "SELL", "q": "2", "z": "2",
                                          "ap": "80000", "T": MS}})  # fmt: skip
    assert b == [("binance", "BTC", NOW, "long", 160000.0)]
    msg = {"topic": "allLiquidation.ETHUSDT",
           "data": [{"T": MS, "s": "ETHUSDT", "S": "Sell", "v": "10", "p": "2500"}]}  # fmt: skip
    y = liq.parse_bybit(msg)
    assert y == [("bybit", "ETH", NOW, "short", 25000.0)]
    assert liq.parse_bybit({"op": "subscribe", "success": True}) == []
    ct = {"BTC-USDT-SWAP": (0.01, False), "BTC-USD-SWAP": (100.0, True)}
    o = liq.parse_okx({"data": [
        {"instId": "BTC-USDT-SWAP", "details": [
            {"posSide": "net", "side": "sell", "sz": "5", "bkPx": "80000", "ts": str(MS)}]},
        {"instId": "BTC-USD-SWAP", "details": [
            {"posSide": "short", "side": "buy", "sz": "3", "bkPx": "80000", "ts": str(MS)}]},
        {"instId": "UNKNOWN-USDT-SWAP", "details": [{"sz": "1", "bkPx": "1", "ts": str(MS)}]},
    ]}, ct)  # fmt: skip
    assert o == [("okx", "BTC", NOW, "long", 4000.0), ("okx", "BTC", NOW, "short", 300.0)]


def test_aggregator_minutes_other_bucket_and_late_messages(engine):
    agg = liq.Aggregator()
    agg.tracked = {"BTC"}
    agg.add("okx", "BTC", NOW + timedelta(seconds=5), "long", 100)
    agg.add("okx", "BTC", NOW + timedelta(seconds=50), "long", 50)
    agg.add("okx", "PEPE", NOW, "short", 7)
    agg.add("okx", "BTC", NOW + timedelta(minutes=1), "long", 1)  # the next, unfinished minute
    rows = agg.take(NOW + timedelta(minutes=1))
    assert sorted((r["symbol"], r["usd"], r["count"]) for r in rows) == [
        ("BTC", 150, 2), ("OTHER", 7, 1),
    ]  # fmt: skip
    lock = threading.Lock()
    with lock, engine.begin() as conn:
        liq.write(conn, rows)
        agg.add("okx", "BTC", NOW + timedelta(seconds=59), "long", 25)  # arrived late
        liq.write(conn, agg.take(NOW + timedelta(minutes=1)))
        got = conn.execute(
            sa.select(crypto_liquidations.c.usd, crypto_liquidations.c.count).where(
                crypto_liquidations.c.symbol == "BTC"
            )
        ).one()
    assert tuple(got) == (175, 3)


def test_totals_normalise_funding():
    t = NOW
    rows = [
        {"ts": t, "oi_usd": 3e9, "funding_8h": history.funding_8h(0.0001, 8)},
        {"ts": t, "oi_usd": 1e9, "funding_8h": history.funding_8h(0.0001, 4)},  # 0.0002 per 8h
        {"ts": t, "oi_usd": None, "funding_8h": 0.5},  # no open interest: not in the sum
    ]
    (total,) = history.totals(rows)
    assert total["oi_usd"] == 4e9 and total["exchanges"] == 2
    assert abs(total["funding_8h"] - 0.000125) < 1e-12


def test_leverage_api(admin, engine):  # noqa: F811
    with engine.begin() as conn:
        conn.execute(crypto_derivs.insert(), [
            {"exchange": ex, "symbol": "BTC", "ts": NOW - timedelta(minutes=5 * i), "oi_usd": oi,
             "oi_coins": 1.0, "funding_rate": 0.0001, "funding_interval_h": 8,
             "long_short_ratio": 1.2, "mark_price": 80000}
            for i in range(3) for ex, oi in (("binance", 2e9), ("bybit", 1e9))
        ])  # fmt: skip
        conn.execute(crypto_liquidations.insert().values(
            exchange="okx", symbol="BTC", minute=NOW - timedelta(minutes=3), side="long",
            usd=1e6, count=4))  # fmt: skip
    q = f"/api/crypto/leverage?asset=btc&to={NOW.isoformat().replace('+00:00', 'Z')}"
    body = admin.get(q).json()
    assert body["asset"] == "BTC" and len(body["snapshots"]) == 6
    assert [t["oi_usd"] for t in body["totals"]] == [3e9] * 3
    assert body["liquidations"][0]["usd"] == 1e6 and body["liquidations"][0]["side"] == "long"
    one = admin.get(q + "&exchange=bybit").json()
    assert {s["exchange"] for s in one["snapshots"]} == {"bybit"} and one["liquidations"] == []
    assert (
        admin.get("/api/crypto/leverage?asset=BTC&from=2026-01-01&to=2026-09-01").status_code == 422
    )
