import sqlalchemy as sa

from tests.conftest import NOW, fake_ctx, news_spec
from trade_news.collectors import cme_fedwatch
from trade_news.collectors.cme_fedwatch import fetch, outcome_label, shifts
from trade_news.db.schema import items, rate_expectations
from trade_news.pipeline import ingest

HOLD, HIKE, HIKE2 = "3.75%-4.00%", "4.00%-4.25%", "4.25%-4.50%"


def snapshot(trade_date, oct_hike, dec=None):
    return {
        "effr": 3.88,
        "current_target": HOLD,
        "trade_date": trade_date,
        "meetings": [
            {"date": "2026-10-28", "probabilities": {HOLD: 100 - oct_hike, HIKE: oct_hike}},
            {"date": "2026-12-09", "probabilities": dec or {HOLD: 6.4, HIKE: 39.0, HIKE2: 54.6}},
        ],
    }


def run(monkeypatch, data, cursor, **params):
    monkeypatch.setattr(cme_fedwatch, "get_probabilities", lambda: data)
    return fetch(fake_ctx(params=params), cursor)


def test_outcome_label():
    assert outcome_label(HOLD, HOLD) == "no change"
    assert outcome_label(HIKE2, HOLD) == "50bp hike"
    assert outcome_label("3.50%-3.75%", HOLD) == "25bp cut"
    assert outcome_label("weird", HOLD) == "weird"


def test_first_run_stores_baseline_without_news(monkeypatch):
    batch = run(monkeypatch, snapshot("2026-09-22", 55.4), None)
    assert batch.items == []
    assert len(batch.rows["rate_expectations"]) == 5  # 2 + 3 outcomes
    assert batch.cursor["trade_date"] == "2026-09-22"


def test_same_trade_date_does_nothing(monkeypatch):
    cursor = {"trade_date": "2026-09-22", "meetings": {}}
    batch = run(monkeypatch, snapshot("2026-09-22", 70.0), cursor)
    assert batch.items == [] and batch.rows == {}
    assert batch.cursor is cursor


def test_big_shift_makes_one_item(monkeypatch):
    first = run(monkeypatch, snapshot("2026-09-22", 55.4), None)
    batch = run(monkeypatch, snapshot("2026-09-23", 70.9), first.cursor)
    assert len(batch.items) == 1
    it = batch.items[0]
    assert it.source_item_id == "fedwatch-2026-09-23"
    assert it.title == (
        "FedWatch: odds of a 25bp hike at the Oct 28, 2026 FOMC meeting rose to 70.9% from 55.4%"
    )
    assert "no change 44.6% -> 29.1%" in it.body
    assert it.raw["hint"].startswith("CME FedWatch")


def test_small_shift_and_far_meetings_are_ignored():
    prev = {"2026-10-28": {HOLD: 50.0, HIKE: 50.0}, "2027-01-27": {HOLD: 50.0, HIKE: 50.0}}
    cur = {"2026-10-28": {HOLD: 45.0, HIKE: 55.0}, "2027-01-27": {HOLD: 10.0, HIKE: 90.0}}
    assert shifts(prev, cur, meetings_ahead=1, min_shift_pp=10) == []
    assert [m["meeting"] for m in shifts(prev, cur, 2, 10)] == ["2027-01-27"]


def test_ingest_stores_snapshots_once(engine, cfg, monkeypatch):
    batch = run(monkeypatch, snapshot("2026-09-22", 55.4), None)
    spec = news_spec("cme_fedwatch", title_dedup=False)
    with engine.begin() as conn:
        assert ingest(conn, spec, batch, cfg, NOW).rows == 5
        assert ingest(conn, spec, batch, cfg, NOW).rows == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(rate_expectations)).scalar() == 5

    later = run(monkeypatch, snapshot("2026-09-23", 70.9), batch.cursor)
    with engine.begin() as conn:
        ingest(conn, spec, later, cfg, NOW)
        row = conn.execute(sa.select(items.c.title, items.c.published_at)).one()
    assert row.title.startswith("FedWatch: odds of a 25bp hike")
    assert row.published_at is not None


def test_shift_towards_no_change(monkeypatch):
    first = run(monkeypatch, snapshot("2026-09-22", 70.9), None)
    it = run(monkeypatch, snapshot("2026-09-23", 55.4), first.cursor).items[0]
    assert it.title.startswith("FedWatch: odds of no change at the Oct 28, 2026 FOMC meeting rose")
