from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from tests.conftest import NOW, news_spec
from tests.test_annotation import SEC_TICKERS, StubLLM, payload
from trade_news.admin.app import AdminDeps, create_app
from trade_news.annotation import assets as ref
from trade_news.annotation.run import annotate_pending
from trade_news.collectors.base import Batch, RawItem
from trade_news.config import LLMConfig, LLMModel
from trade_news.db.schema import (
    annotations,
    asset_aliases,
    asset_resolution_queue,
    assets,
    collector_runs,
    item_assets,
    items,
    subscribers,
)
from trade_news.pipeline import ingest


@pytest.fixture
def admin(engine, cfg):
    with engine.begin() as conn:
        ref.upsert_assets(conn, ref.yaml_assets(), "assets.yaml")
        ref.upsert_assets(conn, ref.sec_equities(SEC_TICKERS), "sec")
        raw = [
            RawItem("1", "Apple beats estimates", "b", "https://example.com/apple", NOW, {}),
            RawItem(
                "2",
                "<script>alert(1)</script> Space startup news",
                "b",
                "javascript:alert(1)",
                NOW,
                {},
            ),
        ]
        ingest(conn, news_spec("finnhub_market_news"), Batch(raw), cfg, NOW)
        conn.execute(
            collector_runs.insert().values(
                source="finnhub_market_news",
                started_at=NOW,
                finished_at=NOW,
                status="ok",
                fetched=2,
                inserted=2,
            )
        )
        conn.execute(
            subscribers.insert().values(
                chat_id=42, chat_type="private", title="@alice", is_active=True, subscribed_at=NOW
            )
        )

    def answer(its):
        out = []
        for it in its:
            if "Space" in it.title:
                link = payload(0)["assets"][0] | {
                    "symbol_or_name": "Sierra Space",
                    "direction": "bearish",
                }
                out.append(payload(it.id, assets=[link], summary="<b>частная</b> компания"))
            else:
                out.append(payload(it.id))
        return out

    annotate_pending(engine, StubLLM(answer), LLMConfig(), NOW)
    cfg.llm.models = [LLMModel(name="model-a", daily_request_limit=300, rpm_limit=15)]
    calls = {"annotate": 0, "reannotate": []}
    deps = AdminDeps(
        engine=engine,
        cfg=cfg,
        sources=[("finnhub_market_news", "Finnhub: общие новости")],
        now=lambda: NOW + timedelta(minutes=5),
        annotate_now=lambda: calls.__setitem__("annotate", calls["annotate"] + 1),
        reannotate=lambda i: calls["reannotate"].append(i),
        bot_running=lambda: True,
    )
    client = TestClient(create_app(deps))
    client.calls = calls
    return client


@pytest.mark.parametrize(
    "path", ["/", "/?hours=168", "/news", "/llm", "/llm?days=30", "/assets", "/subscribers"]
)
def test_pages_render(admin, path):
    r = admin.get(path)
    assert r.status_code == 200, r.text
    assert "trade-news" in r.text


def test_overview_numbers(admin):
    html = admin.get("/").text
    assert "Finnhub: общие новости" in html and "работает" in html
    assert "Apple отчиталась лучше ожиданий" in html  # recent importance >= 3
    assert "model-a" in html


def test_news_filters_detail_and_escaping(admin):
    html = admin.get("/news").text
    assert "&lt;script&gt;" in html and "<script>alert" not in html
    assert "&lt;b&gt;частная&lt;/b&gt;" in html
    assert 'href="javascript:' not in html
    assert "AAPL" in html and "?Sierra Space" in html
    only_bear = admin.get("/news?direction=bearish").text
    assert "Sierra Space" in only_bear and "Apple отчиталась" not in only_bear
    by_ticker = admin.get("/news?q=aapl").text
    assert "Apple отчиталась" in by_ticker and "частная" not in by_ticker


def test_csv_and_raw(admin, engine):
    csv = admin.get("/news.csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.content.startswith(chr(0xFEFF).encode())
    assert "AAPL:bullish:4" in csv.text
    with engine.connect() as conn:
        item_id = conn.execute(sa.select(items.c.id).where(items.c.source_item_id == "1")).scalar()
    assert admin.get(f"/news/{item_id}/raw").json()["summary"] == "Apple отчиталась лучше ожиданий"
    assert admin.get("/news/999999/raw").status_code == 404


def test_reannotate_resets_and_triggers(admin, engine):
    with engine.connect() as conn:
        item_id = conn.execute(sa.select(items.c.id).where(items.c.source_item_id == "1")).scalar()
    r = admin.post(f"/news/{item_id}/reannotate", follow_redirects=False)
    assert r.status_code == 303 and admin.calls["reannotate"] == [item_id]
    with engine.connect() as conn:
        assert not conn.execute(
            sa.select(annotations).where(annotations.c.item_id == item_id)
        ).first()
        assert not conn.execute(
            sa.select(item_assets).where(item_assets.c.item_id == item_id)
        ).first()


def test_annotate_now(admin):
    assert admin.post("/annotate-now", follow_redirects=False).status_code == 303
    assert admin.calls["annotate"] == 1


def test_assets_link_saves_alias_and_resolves(admin, engine):
    html = admin.get("/assets").text
    assert "Sierra Space" in html
    with engine.connect() as conn:
        aapl = conn.execute(sa.select(assets.c.id).where(assets.c.symbol == "AAPL")).scalar()
    r = admin.post(
        "/assets/link",
        data={"name": "Sierra Space", "cls": "equity", "asset_id": aapl, "save_alias": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.connect() as conn:
        link = conn.execute(
            sa.select(item_assets).where(item_assets.c.raw_symbol == "Sierra Space")
        ).one()
        assert link.asset_id == aapl
        assert conn.execute(sa.select(asset_resolution_queue.c.resolved_at)).scalar() is not None
        assert conn.execute(
            sa.select(asset_aliases).where(asset_aliases.c.alias == "Sierra Space")
        ).first()
    assert "Очередь пуста" in admin.get("/assets").text


def test_assets_create_and_ignore(admin, engine):
    r = admin.post(
        "/assets/create",
        data={
            "name": "Sierra Space",
            "cls": "equity",
            "symbol": "sierra",
            "asset_class": "equity",
            "title": "Sierra Space",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.connect() as conn:
        new_id = conn.execute(sa.select(assets.c.id).where(assets.c.symbol == "SIERRA")).scalar()
        assert (
            conn.execute(
                sa.select(item_assets.c.asset_id).where(item_assets.c.raw_symbol == "Sierra Space")
            ).scalar()
            == new_id
        )
    assert (
        admin.post(
            "/assets/create",
            data={"name": "x", "cls": "equity", "symbol": "X", "asset_class": "bogus"},
        ).status_code
        == 400
    )
    assert (
        admin.post(
            "/assets/ignore", data={"name": "nothing", "cls": "equity"}, follow_redirects=False
        ).status_code
        == 303
    )


def test_cross_origin_post_rejected(admin):
    r = admin.post(
        "/annotate-now", headers={"Origin": "https://evil.example"}, follow_redirects=False
    )
    assert r.status_code == 403 and admin.calls["annotate"] == 0
    ok = admin.post(
        "/annotate-now", headers={"Origin": "http://testserver"}, follow_redirects=False
    )
    assert ok.status_code == 303


def test_subscribers_page(admin):
    html = admin.get("/subscribers").text
    assert "@alice" in html and "бот работает" in html
    assert "Правила рассылки" in html and "выключена" in html
