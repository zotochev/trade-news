"""Background digest: selection below the delivery thresholds, grouping, sending, the window."""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from tests.conftest import NOW, news_spec
from tests.test_annotation import SEC_TICKERS, StubLLM, payload
from trade_news import background, delivery
from trade_news.annotation import assets as ref
from trade_news.annotation.run import annotate_pending
from trade_news.collectors.base import Batch, RawItem
from trade_news.config import BackgroundConfig, LLMConfig
from trade_news.db.schema import items, subscribers
from trade_news.pipeline import ingest

CFG = BackgroundConfig()
OWNER = 1


def link(symbol, importance, direction="bullish", scope="specific"):
    return {
        "asset_class": "equity", "scope": scope, "symbol_or_name": symbol, "group_label": None,
        "direction": direction, "importance": importance, "is_primary": True, "confidence": 0.8,
    }  # fmt: skip


ANSWERS = {
    "apple-up": {"assets": [link("AAPL", 3)], "sectors": ["hardware"]},
    "apple-down": {"assets": [link("AAPL", 2, "bearish")], "sectors": ["hardware"]},
    "msft-earnings": {
        "assets": [link("MSFT", 5)],
        "sectors": ["software"],
        "event_type": "earnings",
    },  # passes the delivery rules: not background
    "google": {"assets": [link("GOOGL", 3)], "sectors": ["internet"]},
    "market": {"assets": [link(None, 3, "bearish", "market_wide")], "sectors": []},
    "noise": {"assets": [link("AAPL", 1)], "sectors": ["hardware"]},
}


@pytest.fixture
def world(engine, cfg):
    with engine.begin() as conn:
        ref.upsert_assets(conn, ref.yaml_assets(), "assets.yaml")
        ref.upsert_assets(conn, ref.sec_equities(SEC_TICKERS), "sec")
        raw = [RawItem(k, f"{k} story", "b", f"https://x.com/{k}", NOW, {}) for k in ANSWERS]
        ingest(conn, news_spec("n", title_dedup=False), Batch(raw), cfg, NOW)
        keys = dict(conn.execute(sa.select(items.c.id, items.c.source_item_id)).all())
        delivery.save_rules(
            conn,
            delivery.DeliveryRules(
                enabled=True, min_importance=4, require_direction=False, event_types=[]
            ),
            NOW,
        )
        conn.execute(
            subscribers.insert().values(
                chat_id=42, chat_type="private", title="@a", is_active=True, subscribed_at=NOW
            )
        )
    llm = StubLLM(
        lambda its: [
            payload(i.id, summary=f"{keys[i.id]} суть", **ANSWERS[keys[i.id]]) for i in its
        ]
    )
    annotate_pending(engine, llm, LLMConfig(batch_size=10), NOW + timedelta(minutes=1))
    return engine


def test_selection_and_grouping(world):
    with world.connect() as conn:
        found = background.window_items(conn, NOW, NOW + timedelta(hours=1), CFG.min_importance)
    assert {f["summary"] for f in found} == {
        "apple-up суть", "apple-down суть", "google суть", "market суть",
    }  # fmt: skip
    sections = background.group(found)
    assert [s.title for s in sections] == ["#электроника", background.OTHER, background.NO_SECTOR]
    apple = sections[0].assets["AAPL"]
    assert apple.arrows == ["▲", "▼"] and apple.best["summary"] == "apple-up суть"
    (text,) = background.format_sections(sections, NOW, NOW + timedelta(hours=4), 5)
    assert "🗂 <b>Фон рынка</b> · 18:00–22:00 UTC · 4 новости" in text
    assert "<b>#электроника</b> · 2 ▲1 ▼1\nAAPL ▲▼ (2) — " in text
    assert '<a href="https://x.com/apple-up">apple-up суть</a>' in text
    assert "GOOGL ▲ — " in text and "#интернет" in text  # single item: tag inside the line
    assert "акции в целом ▼ — " in text


def test_long_digest_is_split_by_visible_length():
    blocks = [f'<a href="https://example.com/{"x" * 200}">{"я" * 90}</a>' for _ in range(80)]
    messages = background._pack("head", blocks)
    assert len(messages) > 1
    assert all(background.visible_len(m) <= 4096 for m in messages)
    assert sum(m.count("<a ") for m in messages) == 80


def test_run_sends_once_per_window(world):
    sent = []

    def api(method, **kw):
        sent.append(kw["chat_id"])
        return {"message_id": len(sent)}

    at = NOW + timedelta(hours=2)
    assert background.run_background(world, api, OWNER, CFG, lambda: at) == 2
    assert sorted(sent) == [OWNER, 42]
    # the next run starts where this one ended: nothing new, nothing sent
    later = at + timedelta(hours=4)
    assert background.run_background(world, api, OWNER, CFG, lambda: later) == 0


def test_disabled_delivery_sends_nothing(world):
    with world.begin() as conn:
        delivery.save_rules(conn, delivery.DeliveryRules(enabled=False), NOW)
    calls = []
    at = NOW + timedelta(hours=2)
    assert background.run_background(world, lambda m, **kw: calls.append(kw), OWNER, CFG,
                                     lambda: at) == 0  # fmt: skip
    assert calls == []
