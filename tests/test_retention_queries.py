from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from tests.conftest import NOW, news_spec
from tests.test_annotation import SEC_TICKERS, StubLLM, payload
from trade_news import queries
from trade_news.annotation import assets as ref
from trade_news.annotation.run import annotate_pending
from trade_news.collectors.base import Batch, RawItem
from trade_news.config import LLMConfig, RetentionConfig
from trade_news.db.schema import annotations, collector_runs, item_assets, items, raw_items
from trade_news.pipeline import ingest
from trade_news.retention import cleanup


def add(engine, cfg, source, sid, title, fetched, url=None):
    with engine.begin() as conn:
        ingest(
            conn,
            news_spec(source),
            Batch([RawItem(sid, title, "b", url, fetched, {})]),
            cfg,
            fetched,
        )


def n(engine, table):
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar()


def test_cleanup_by_age_and_annotation(engine, cfg):
    old = NOW - timedelta(days=40)
    add(engine, cfg, "a", "1", "Old unannotated story about something", old, "https://x.com/1")
    add(engine, cfg, "b", "2", "Old duplicate from another source", old, "https://x.com/1")
    add(engine, cfg, "a", "3", "Old but annotated story about Apple", old)
    add(engine, cfg, "a", "4", "Fresh story that stays", NOW)
    with engine.begin() as conn:
        conn.execute(
            collector_runs.insert().values(
                source="a", started_at=NOW - timedelta(days=100), status="ok"
            )
        )
    annotate_pending(
        engine, StubLLM(), LLMConfig(max_item_age_hours=24 * 60), old + timedelta(hours=1)
    )
    assert n(engine, annotations) == 3  # group leaders "1", "3", "4"

    # make group "1" unannotated
    with engine.begin() as conn:
        leader_1 = conn.execute(sa.select(items.c.id).where(items.c.source_item_id == "1")).scalar()
        conn.execute(item_assets.delete().where(item_assets.c.item_id == leader_1))
        for t in ("item_relevance", "asset_resolution_queue"):
            conn.execute(sa.text(f"delete from {t} where item_id = :i"), {"i": leader_1})
        conn.execute(annotations.delete().where(annotations.c.item_id == leader_1))

    stats = cleanup(
        engine, RetentionConfig(unannotated_days=30, annotated_days=365, logs_days=90), NOW
    )
    assert (stats.groups, stats.items) == (1, 2)  # the whole group "1" incl. its duplicate
    with engine.connect() as conn:
        left = set(conn.execute(sa.select(items.c.source_item_id)).scalars())
    assert left == {"3", "4"}
    assert n(engine, raw_items) == 2 and n(engine, collector_runs) == 0

    stats = cleanup(engine, RetentionConfig(unannotated_days=30, annotated_days=20), NOW)
    assert stats.items == 1 and n(engine, annotations) == 1  # "3": annotated, but past its limit


def test_target_queries(engine, cfg):
    with engine.begin() as conn:
        ref.upsert_assets(conn, ref.yaml_assets(), "assets.yaml")
        ref.upsert_assets(conn, ref.sec_equities(SEC_TICKERS), "sec")
    add(engine, cfg, "a", "1", "Apple beats estimates and raises guidance", NOW)
    add(engine, cfg, "a", "2", "ECB meeting next Thursday could move the euro", NOW)
    fx_link = {
        "asset_class": "fx", "scope": "specific", "symbol_or_name": "EUR/USD", "group_label": None,
        "direction": "bearish", "importance": 5, "is_primary": True, "confidence": 0.8,
    }  # fmt: skip
    fx_wide = fx_link | {"scope": "market_wide", "symbol_or_name": None, "importance": 3}
    scheduled = {
        "type": "scheduled", "date_iso": "2026-10-01", "date_to_iso": None,
        "date_precision": "day", "raw_phrase": "next Thursday",
    }  # fmt: skip

    def answer(its):
        out = []
        for it in its:
            if "ECB" in it.title:
                out.append(payload(it.id, assets=[fx_link, fx_wide], relevance=scheduled))
            else:
                out.append(payload(it.id))
        return out

    annotate_pending(engine, StubLLM(answer), LLMConfig(), NOW)
    with engine.connect() as conn:

        def run(q):
            return conn.execute(q).all()

        aapl = run(queries.asset_news("AAPL", NOW - timedelta(days=1), NOW + timedelta(hours=1)))
        assert [r.title for r in aapl] == ["Apple beats estimates and raises guidance"]
        oct1 = datetime(2026, 10, 1, tzinfo=UTC).date()
        fx = run(queries.class_on_day("fx", oct1))
        assert {r.scope for r in fx} == {"specific", "market_wide"}
        assert run(queries.class_on_day("fx", NOW.date())) == []
        up = run(queries.asset_upcoming("EUR/USD", NOW, days=14))
        assert [r.importance for r in up] == [5]
        assert run(queries.asset_upcoming("EURUSD", NOW, days=3)) == []
        sched = run(queries.scheduled_future(NOW))
        assert {r.relevance_type for r in sched} == {"scheduled"} and len(sched) == 2
