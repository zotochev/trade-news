from dataclasses import replace
from datetime import UTC, date, datetime

import httpx
import pytest
import sqlalchemy as sa

from tests.conftest import NOW, fake_ctx, news_spec
from trade_news.collectors.ff_calendar import FEED_URL, compute_actual, fetch, parse_value
from trade_news.db.schema import econ_events
from trade_news.pipeline import ingest

CPI = {
    "title": "CPI m/m",
    "country": "USD",
    "date": "2026-09-24T08:30:00-04:00",  # 12:30 UTC, the day after NOW
    "impact": "High",
    "forecast": "0.3%",
    "previous": "0.2%",
}
LOW = {**CPI, "title": "Some Low Event", "impact": "Low"}
SNB = {**CPI, "title": "SNB Policy Rate", "country": "CHF", "forecast": "0.00%"}
MAPPING = {"title": "CPI m/m", "series": "CPIAUCSL", "calc": "mom_pct", "decimals": 1}
AFTER = datetime(2026, 9, 24, 13, 0, tzinfo=UTC)


class Fake:
    def __init__(self, events, fred):
        self.events, self.fred, self.fred_calls = events, fred, 0

    def get(self, url, **kw):
        if url == FEED_URL:
            assert "User-Agent" in kw["headers"]
            return httpx.Response(200, json=self.events)
        self.fred_calls += 1
        rows = [{"date": d, "value": str(v)} for d, v in self.fred]
        return httpx.Response(200, json={"observations": rows})


def run(fake, cursor, now=NOW, **params):
    ctx = fake_ctx(
        get=fake.get,
        params={"fred_actuals": [MAPPING], **params},
        secrets={"FRED_API_KEY": "k"},
    )
    return fetch(replace(ctx, now=lambda: now), cursor)


def test_parse_value():
    assert parse_value("21.5K") == (21.5, "K")
    assert parse_value("-15.8K") == (-15.8, "K")
    assert parse_value("4.5%") == (4.5, "%")
    assert parse_value("<0.1%") == (0.1, "%")
    assert parse_value("47.4") == (47.4, None)
    assert parse_value("") == (None, None)


def test_compute_actual():
    obs = [(date(2025, 8, 1), 100.0), (date(2026, 7, 1), 102.0), (date(2026, 8, 1), 102.408)]
    assert round(compute_actual(obs, "mom_pct"), 1) == 0.4
    assert round(compute_actual(obs, "yoy_pct"), 1) == 2.4
    assert compute_actual(obs, "diff", 1000) == pytest.approx(408.0)
    assert compute_actual(obs, "level") == 102.408
    assert compute_actual(obs[:1], "mom_pct") is None


def test_release_cycle():
    fake = Fake([CPI, LOW, SNB], [("2026-06-01", 101.8), ("2026-07-01", 102.0)])

    first = run(fake, None)  # before the release: snapshots + FRED baseline
    assert [r["event_name"] for r in first.rows["econ_events"]] == ["CPI m/m", "SNB Policy Rate"]
    assert first.rows["econ_events"][0]["forecast"] == 0.3
    assert first.rows["econ_events"][0]["unit"] == "%"
    assert first.items == [] and fake.fred_calls == 1

    again = run(fake, first.cursor)  # nothing changed: no rows, no FRED call
    assert again.rows["econ_events"] == [] and fake.fred_calls == 1

    waiting = run(fake, again.cursor, now=AFTER)  # released, FRED not updated yet
    assert waiting.items == [] and fake.fred_calls == 2

    fake.fred.append(("2026-08-01", 102.408))
    done = run(fake, waiting.cursor, now=AFTER)
    (it,) = done.items
    assert it.title == "USD CPI m/m: actual 0.4% vs forecast 0.3%, previous 0.2%"
    assert "surprise +0.1%" in it.body
    assert it.source_item_id == "USD|CPI m/m|2026-09-24T08:30:00-04:00"
    assert done.rows["econ_events"][0]["actual"] == 0.4

    later = run(fake, done.cursor, now=AFTER)  # done: no second item
    assert later.items == [] and fake.fred_calls == 3


def test_forecast_revision_makes_new_snapshot():
    fake = Fake([CPI], [("2026-07-01", 102.0)])
    first = run(fake, None)
    fake.events = [{**CPI, "forecast": "0.4%"}]
    assert [r["forecast"] for r in run(fake, first.cursor).rows["econ_events"]] == [0.4]


def test_first_seen_after_release_is_skipped():
    fake = Fake([CPI], [("2026-08-01", 102.4)])
    batch = run(fake, None, now=AFTER)
    assert batch.items == [] and fake.fred_calls == 0
    assert batch.cursor["events"]["USD|CPI m/m|2026-09-24T08:30:00-04:00"]["done"]


def test_gives_up_after_window():
    fake = Fake([CPI], [("2026-07-01", 102.0)])
    cursor = run(fake, None).cursor
    late = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)  # 12.5 h after the release
    batch = run(fake, cursor, now=late)
    assert batch.items == []
    assert batch.cursor["events"]["USD|CPI m/m|2026-09-24T08:30:00-04:00"]["done"]


def test_old_events_dropped_from_cursor():
    cursor = {"events": {"USD|Old|2026-09-01T08:30:00-04:00": {"done": True}}}
    batch = run(Fake([], []), cursor)
    assert batch.cursor == {"events": {}}


def test_ingest_appends_snapshots(engine, cfg):
    fake = Fake([CPI], [("2026-07-01", 102.0)])
    batch = run(fake, None)
    spec = news_spec("ff_calendar", title_dedup=False)
    with engine.begin() as conn:
        assert ingest(conn, spec, batch, cfg, NOW).rows == 1
        fake.fred.append(("2026-08-01", 102.408))
        done = run(fake, batch.cursor, now=AFTER)
        ingest(conn, spec, done, cfg, NOW)
        rows = conn.execute(
            sa.select(econ_events.c.actual, econ_events.c.surprise).order_by(econ_events.c.id)
        ).all()
    assert rows[0].actual is None
    assert rows[1].actual == 0.4 and rows[1].surprise == pytest.approx(0.1)
