from datetime import timedelta

import pytest
import sqlalchemy as sa

from tests.conftest import NOW, news_spec
from trade_news.collectors.base import Batch, RawItem
from trade_news.db.schema import items, raw_items
from trade_news.dedup import normalize_title, normalize_url
from trade_news.pipeline import ingest


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "http://www.Reuters.com/markets/fed-holds/?utm_source=x&b=2&a=1#top",
            "https://reuters.com/markets/fed-holds?a=1&b=2",
        ),
        ("https://example.com/a?fbclid=123", "https://example.com/a"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_url(url, expected):
    assert normalize_url(url) == expected


def test_normalize_title():
    assert (
        normalize_title("  Fed HOLDS rates — steady; “data-dependent”! ")
        == "fed holds rates steady data dependent"
    )
    assert normalize_title("Биткоин  вырос!") == "биткоин вырос"


def item(sid, title, url=None, published=NOW, body=None) -> RawItem:
    return RawItem(sid, title, body, url, published, {"id": sid})


def run(engine, cfg, spec, *raw):
    with engine.begin() as conn:
        return ingest(conn, spec, Batch(list(raw)), cfg, fetched_at=NOW)


def groups(engine) -> dict[tuple[str, str], tuple[int, str | None]]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                items.c.source,
                items.c.source_item_id,
                items.c.id,
                items.c.dedup_group_id,
                items.c.dedup_reason,
            )
        )
        return {(r.source, r.source_item_id): (r.dedup_group_id, r.dedup_reason) for r in rows}


def test_same_url_different_sources_grouped(engine, cfg):
    run(
        engine,
        cfg,
        news_spec("a"),
        item("1", "Apple beats estimates", "https://www.cnbc.com/apple?utm_source=a"),
    )
    stats = run(
        engine,
        cfg,
        news_spec("b"),
        item("x", "Totally different headline here", "https://cnbc.com/apple/"),
    )
    g = groups(engine)
    assert stats.dedup_merged == 1
    assert g[("b", "x")] == (g[("a", "1")][0], "url")


def test_same_title_grouped_by_hash(engine, cfg):
    run(engine, cfg, news_spec("a"), item("1", "Fed holds rates steady, signals patience"))
    run(engine, cfg, news_spec("b"), item("2", "FED holds rates steady — signals patience!"))
    assert groups(engine)[("b", "2")][1] == "title_hash"


def test_fuzzy_title_grouped(engine, cfg):
    run(
        engine,
        cfg,
        news_spec("a"),
        item("1", "Nvidia shares jump after record quarterly revenue forecast"),
    )
    run(
        engine,
        cfg,
        news_spec("b"),
        item("2", "Nvidia shares jump after record quarterly revenue forecasts"),
    )
    run(engine, cfg, news_spec("c"), item("3", "Oil prices slide as OPEC+ weighs output increase"))
    g = groups(engine)
    assert g[("b", "2")] == (g[("a", "1")][0], "fuzzy")
    assert g[("c", "3")] == (g[("c", "3")][0], None)
    assert g[("c", "3")][0] != g[("a", "1")][0]


def test_outside_window_not_grouped(engine, cfg):
    run(engine, cfg, news_spec("a"), item("1", "Fed holds rates steady, signals patience"))
    later = NOW + timedelta(hours=cfg.dedup.window_hours, minutes=1)
    run(
        engine,
        cfg,
        news_spec("b"),
        item("2", "Fed holds rates steady, signals patience", published=later),
    )
    assert groups(engine)[("b", "2")][1] is None


def test_short_titles_not_fuzzy_matched(engine, cfg):
    run(engine, cfg, news_spec("a"), item("1", "Stocks up"))
    run(engine, cfg, news_spec("b"), item("2", "Stocks up!"))
    assert groups(engine)[("b", "2")][1] is None


def test_templated_filing_titles_never_title_grouped(engine, cfg):
    edgar = news_spec("sec_edgar", title_dedup=False)
    run(
        engine,
        cfg,
        edgar,
        item("0001-26-1", "8-K - Apple Inc. (0000320193) (Filer)", "https://sec.gov/a"),
        item("0001-26-2", "8-K - Apple Inc. (0000320193) (Filer)", "https://sec.gov/b"),
    )
    # and a news item with the same wording is not absorbed by the filing either
    run(engine, cfg, news_spec("news"), item("n1", "8-K - Apple Inc. (0000320193) (Filer)"))
    g = groups(engine)
    assert len({gid for gid, _ in g.values()}) == 3


def test_repeated_fetch_is_idempotent(engine, cfg):
    spec = news_spec("a")
    first = run(engine, cfg, spec, item("1", "Some headline long enough to matter"))
    again = run(
        engine,
        cfg,
        spec,
        item("1", "Some headline long enough to matter"),
        item("1", "Some headline long enough to matter"),
    )
    assert (first.inserted, again.inserted, again.seen_before) == (1, 0, 2)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(raw_items)).scalar() == 1
        assert conn.execute(sa.select(sa.func.count()).select_from(items)).scalar() == 1


def test_revision_keeps_raw_history_and_updates_item(engine, cfg):
    spec = news_spec("a")
    run(engine, cfg, spec, item("1", "Company X to acquire Y for $1bn", body="v1"))
    stats = run(engine, cfg, spec, item("1", "Company X to acquire Y for $1.2bn", body="v2"))
    assert (stats.inserted, stats.dedup_merged) == (1, 0)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(raw_items)).scalar() == 2
        row = conn.execute(sa.select(items)).one()
    assert (row.title, row.body) == ("Company X to acquire Y for $1.2bn", "v2")


def test_group_leader_is_first_item_even_via_chain(engine, cfg):
    run(
        engine,
        cfg,
        news_spec("a"),
        item("1", "Tesla recalls 100,000 vehicles over faulty seatbelt warning", "https://x.com/1"),
    )
    run(
        engine,
        cfg,
        news_spec("b"),
        item("2", "Different words entirely, same link", "https://x.com/1"),
    )
    run(engine, cfg, news_spec("c"), item("3", "Different words entirely, same link!"))
    g = groups(engine)
    leader = g[("a", "1")][0]
    assert g[("b", "2")][0] == leader and g[("c", "3")][0] == leader
