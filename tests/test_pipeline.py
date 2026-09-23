from datetime import datetime

import httpx
import pytest
import respx
import sqlalchemy as sa

from tests.conftest import NOW, fake_ctx, news_spec
from trade_news.collectors.base import Batch, RawItem
from trade_news.db.schema import collector_runs, collector_state, items
from trade_news.http import RateLimiter, make_getter
from trade_news.pipeline import run_source


def test_failing_collector_is_isolated_and_recorded(engine, cfg):
    def boom(ctx, cursor):
        raise RuntimeError("source is down")

    def ok(ctx, cursor):
        return Batch([RawItem("1", "Headline that is long enough", None, None, NOW, {})], {"n": 1})

    assert run_source(engine, news_spec("bad", fetch=boom), cfg, fake_ctx()) is None
    stats = run_source(engine, news_spec("good", fetch=ok), cfg, fake_ctx())
    assert stats.inserted == 1
    with engine.connect() as conn:
        runs = {r.source: r for r in conn.execute(sa.select(collector_runs))}
        assert runs["bad"].status == "error" and "source is down" in runs["bad"].error
        assert runs["good"].status == "ok"
        assert conn.execute(sa.select(collector_state.c.cursor)).scalar() == {"n": 1}


def test_cursor_is_passed_back_on_next_run(engine, cfg):
    received = []

    def fetch(ctx, cursor):
        received.append(cursor)
        return Batch([], {"n": len(received)})

    spec = news_spec("s", fetch=fetch)
    run_source(engine, spec, cfg, fake_ctx())
    run_source(engine, spec, cfg, fake_ctx())
    assert received == [None, {"n": 1}]


def test_datetimes_roundtrip_as_utc_and_naive_rejected(engine, cfg):
    def fetch(ctx, cursor):
        aware = datetime.fromisoformat("2026-09-23T14:03:26-04:00")
        return Batch([RawItem("1", "Headline that is long enough", None, None, aware, {})])

    run_source(engine, news_spec("s", fetch=fetch), cfg, fake_ctx())
    with engine.connect() as conn:
        published = conn.execute(sa.select(items.c.published_at)).scalar()
    assert published.isoformat() == "2026-09-23T18:03:26+00:00"

    def naive(ctx, cursor):
        return Batch(
            [RawItem("2", "Another long enough headline", None, None, datetime(2026, 1, 1), {})]
        )

    assert run_source(engine, news_spec("n", fetch=naive), cfg, fake_ctx()) is None


@respx.mock
def test_getter_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    route = respx.get("https://api.test/x").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(503),
            httpx.Response(200, json=[1]),
        ]
    )
    with httpx.Client() as client:
        get = make_getter(client, RateLimiter(100, 1), source="t")
        assert get("https://api.test/x").json() == [1]
    assert route.call_count == 3


@respx.mock
def test_getter_does_not_retry_client_errors(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    route = respx.get("https://api.test/x").mock(return_value=httpx.Response(401))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        make_getter(client, RateLimiter(100, 1), source="t")("https://api.test/x")
    assert route.call_count == 1


def test_rate_limiter_blocks_when_window_full(monkeypatch):
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr("time.monotonic", lambda: clock[0])

    def fake_sleep(s):
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr("time.sleep", fake_sleep)
    rl = RateLimiter(calls=2, period=10)
    rl.acquire()
    rl.acquire()
    rl.acquire()
    assert sleeps == [10]
