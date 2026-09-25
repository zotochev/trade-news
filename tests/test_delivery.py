from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa

from tests.conftest import NOW, news_spec
from tests.test_admin import admin  # noqa: F401  (fixture)
from tests.test_annotation import SEC_TICKERS, StubLLM, payload
from trade_news import delivery
from trade_news.annotation import assets as ref
from trade_news.annotation.run import annotate_pending
from trade_news.collectors.base import Batch, RawItem
from trade_news.config import LLMConfig
from trade_news.db.schema import deliveries, subscribers
from trade_news.delivery import DeliveryRules, passes, run_delivery
from trade_news.pipeline import ingest
from trade_news.telegram.api import TelegramError
from trade_news.telegram.message import format_digest, format_item

OWNER, ALICE = 111, 222


def link(**kw):
    base = {
        "asset_class": "equity", "scope": "specific", "symbol": "AAPL", "direction": "bullish",
        "importance": 3, "is_primary": True,
    }  # fmt: skip
    return base | kw


@pytest.mark.parametrize(
    ("links", "event", "ok"),
    [
        ([link()], "earnings", True),
        ([link(importance=2)], "earnings", False),
        ([link(direction="neutral")], "earnings", False),
        ([link()], "other", False),  # not a material event
        ([link(asset_class="crypto", symbol="BTC")], "earnings", True),
        (
            [link(importance=2, is_primary=True), link(importance=5, is_primary=False)],
            "macro",
            False,
        ),
        ([link(importance=2, symbol="NVDA")], "other", True),  # watchlist overrides the rest
        ([], "earnings", False),
    ],
)
def test_passes(links, event, ok):
    rules = DeliveryRules(watchlist=["nvda"], watchlist_min_importance=2)
    assert passes(rules, links, event) is ok


def test_rules_roundtrip_default_off(engine):
    with engine.begin() as conn:
        assert delivery.load_rules(conn).enabled is False
        delivery.save_rules(conn, DeliveryRules(enabled=True, min_importance=4), NOW)
        delivery.save_rules(conn, DeliveryRules(enabled=True, min_importance=5), NOW)
        assert delivery.load_rules(conn).min_importance == 5


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.fail: dict[int, Exception] = {}  # chat_id → exception to raise (once per call)
        self.down = False

    def __call__(self, method, **p):
        assert method == "sendMessage"
        if self.down:
            raise httpx.ConnectError("telegram is down")
        if (exc := self.fail.get(p["chat_id"])) is not None:
            raise exc
        self.sent.append((p["chat_id"], p["text"]))
        return {"message_id": len(self.sent)}

    def to(self, chat):
        return [t for c, t in self.sent if c == chat]


@pytest.fixture
def world(engine, cfg):
    """3 annotated items: AAPL bullish 4 (earnings), MSFT bearish 3 (guidance), GOOGL neutral 3."""
    with engine.begin() as conn:
        ref.upsert_assets(conn, ref.sec_equities(SEC_TICKERS), "sec")
        raw = [
            RawItem(
                str(i),
                [
                    "Apple beats on earnings",
                    "Microsoft cuts its outlook",
                    "Alphabet holds a press event",
                ][i],
                "b",
                f"https://x.com/{i}",
                NOW,
                {},
            )
            for i in range(3)
        ]
        ingest(conn, news_spec("news"), Batch(raw), cfg, NOW)
        conn.execute(
            subscribers.insert().values(
                chat_id=ALICE,
                chat_type="private",
                title="@alice",
                is_active=True,
                subscribed_at=NOW,
            )
        )
    base = payload(0)["assets"][0]

    def answer(its):
        out = []
        for it in its:
            n = ["Apple", "Microsoft", "Alphabet"].index(it.title.split()[0])
            a = [
                base | {"importance": 4, "symbol_or_name": "AAPL"},
                base | {"importance": 3, "symbol_or_name": "MSFT", "direction": "bearish"},
                base | {"importance": 3, "symbol_or_name": "GOOGL", "direction": "neutral"},
            ][n]
            ev = ["earnings", "guidance", "earnings"][n]
            out.append(payload(it.id, assets=[a], event_type=ev, summary=f"Суть {n} <важно>"))
        return out

    annotate_pending(engine, StubLLM(answer), LLMConfig(), NOW)
    return engine


def enable(engine, **kw):
    with engine.begin() as conn:
        delivery.save_rules(conn, DeliveryRules(enabled=True, **kw), NOW)


def statuses(engine):
    with engine.connect() as conn:
        return sorted(conn.execute(sa.select(deliveries.c.channel, deliveries.c.status)).all())


def test_disabled_sends_nothing(world):
    tg = FakeTelegram()
    assert run_delivery(world, tg, OWNER, lambda: NOW).sent == 0
    assert tg.sent == []


def test_sends_to_owner_and_subscribers_once(world):
    enable(world)
    tg = FakeTelegram()
    stats = run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=1))
    assert (stats.candidates, stats.sent) == (2, 4)  # 2 items (neutral one filtered) × 2 chats
    assert stats.messages == 2  # one digest per chat
    assert len(tg.to(OWNER)) == 1 and len(tg.to(ALICE)) == 1
    (digest,) = tg.to(OWNER)
    assert digest.startswith("📰 <b>Сводка новостей</b>")
    assert "📈 Отчётность · <b>AAPL ▲</b> · важность 4/5" in digest
    assert "🎯 Прогноз компании · <b>MSFT ▼</b>" in digest
    assert "&lt;важно&gt;" in digest  # summary is escaped for HTML
    again = run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=2))
    assert again.sent == 0 and len(tg.sent) == 2


def test_priority_item_goes_alone_and_first(world):
    enable(world, always_event_types=["guidance"])  # makes the MSFT item a priority one
    tg = FakeTelegram()
    run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=1))
    first, second = tg.to(OWNER)
    assert first.startswith("🎯 Прогноз компании · <b>MSFT ▼</b>")
    assert second.startswith("📈 Отчётность · <b>AAPL ▲</b>")  # the only other one: no digest


def test_hourly_cap_defers_then_sends(world):
    # the cap counts messages: MSFT (priority) goes alone, AAPL waits for the next hour
    enable(world, max_per_hour=1, always_event_types=["guidance"])
    tg = FakeTelegram()
    s1 = run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=1))
    assert (s1.sent, s1.deferred) == (2, 2)  # per chat: MSFT sent, AAPL deferred
    assert all("MSFT" in text for _, text in tg.sent)
    s2 = run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=30))
    assert s2.sent == 0
    s3 = run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=62))
    assert s3.sent == 2 and len(tg.sent) == 4


def test_blocked_chat_is_unsubscribed_others_still_get_it(world):
    enable(world)
    tg = FakeTelegram()
    tg.fail[ALICE] = TelegramError(
        "sendMessage", 403, "Forbidden: bot was blocked by the user", None
    )
    run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=1))
    assert len(tg.to(OWNER)) == 1  # the digest with both items
    with world.connect() as conn:
        assert conn.execute(sa.select(subscribers.c.is_active)).scalar() is False


def test_telegram_down_keeps_rows_pending_and_retries(world):
    enable(world)
    tg = FakeTelegram()
    tg.down = True
    stats = run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=1))
    assert stats.stopped == "telegram_unavailable" and stats.sent == 0
    assert ("111", "pending") in statuses(world)
    tg.down = False
    assert run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=2)).sent == 4


def test_stale_items_are_never_sent(world):
    enable(world, max_age_hours=1)
    tg = FakeTelegram()
    tg.down = True
    run_delivery(world, tg, OWNER, lambda: NOW + timedelta(minutes=1))
    tg.down = False
    stats = run_delivery(world, tg, OWNER, lambda: NOW + timedelta(hours=2))
    assert stats.sent == 0 and stats.skipped >= 1
    assert all(s == "skipped" for _, s in statuses(world))


def test_bad_html_falls_back_to_plain_text(world):
    enable(world)
    calls = []

    def api(method, **p):
        calls.append(p)
        if p.get("parse_mode") == "HTML":
            raise TelegramError("sendMessage", 400, "Bad Request: can't parse entities", None)
        return {"message_id": 1}

    assert run_delivery(world, api, OWNER, lambda: NOW + timedelta(minutes=1)).sent == 4
    assert "<b>" not in calls[1]["text"]


def test_format_item_only_links_http():
    item = {
        "payload_json": {"summary": "Суть"},
        "links": [
            link(importance=4),
            link(symbol=None, scope="market_wide", asset_class="equity", is_primary=False),
        ],
        "relevance": None,
        "source": "sec_edgar",
        "raw_url": "javascript:alert(1)",
    }
    text = format_item(item)
    assert "javascript" not in text and "Источник: SEC EDGAR" in text
    assert "Также: акции в целом ▲ 3" in text


def test_admin_rules_preview_and_save(admin, engine):  # noqa: F811
    form = {
        "enabled": "1", "min_importance": "4", "require_direction": "1",
        "event_types": ["earnings"], "asset_classes": ["equity"], "watchlist": "aapl, nvda",
        "watchlist_min_importance": "3", "max_per_hour": "5", "max_age_hours": "6",
    }  # fmt: skip
    r = admin.post("/delivery/rules", data=form | {"action": "preview"})
    assert r.status_code == 200 and "не сохранены" in r.text
    with engine.connect() as conn:
        assert delivery.load_rules(conn).enabled is False
    r = admin.post("/delivery/rules", data=form | {"action": "save"}, follow_redirects=False)
    assert r.status_code == 303
    with engine.connect() as conn:
        saved = delivery.load_rules(conn)
    assert saved.enabled and saved.min_importance == 4 and saved.watchlist == ["AAPL", "NVDA"]
    bad = admin.post("/delivery/rules", data=form | {"max_per_hour": "999", "action": "save"})
    assert "не сохранены" in bad.text


def test_priority_sources_skip_thresholds():
    rules = DeliveryRules(min_importance=5)
    weak = [link(importance=1, direction="neutral")]
    assert passes(rules, weak, "macro", "ff_calendar")
    assert passes(rules, weak, "rate_decision", "fed_rss")
    assert not passes(rules, weak, "macro", "finnhub_market_news")
    assert not passes(DeliveryRules(min_importance=5, always_sources=[]), weak, "macro", "fred")


def test_format_calendar_release():
    item = {
        "source": "ff_calendar",
        "payload_json": {"summary": "Инфляция выше прогноза", "event_type": "macro"},
        "raw_json": {"actual": 0.4, "forecast": "0.3%", "previous": "0.2%"},
        "links": [link(symbol="USD", asset_class="fx", importance=4)],
        "relevance": {"relevance_type": "immediate", "relevant_from": NOW},
        "raw_url": "https://fred.stlouisfed.org/series/CPIAUCSL",
    }
    text = format_item(item)
    assert text.startswith("📊 Макроданные · <b>USD ▲</b> · важность 4/5\nИнфляция выше прогноза")
    assert "Факт 0.4% · прогноз 0.3% · пред. 0.2% · сюрприз +0.1%" in text
    assert "Когда" not in text  # "immediate" would only repeat the message time
    assert text.endswith(
        'Источник: <a href="https://fred.stlouisfed.org/series/CPIAUCSL">Календарь + FRED</a>'
    )


def test_format_fedwatch_and_scheduled_when():
    item = {
        "source": "cme_fedwatch",
        "payload_json": {"summary": "Рынок больше ждёт повышения", "event_type": "rate_decision"},
        "raw_json": {
            "target": "3.75%-4.00%",
            "shifts": [
                {"meeting": "2026-10-28", "outcome": "4.00%-4.25%", "before": 55.4, "after": 70.9}
            ],
        },
        "links": [link(symbol=None, scope="market_wide", asset_class="rates", importance=3)],
        "relevance": {
            "relevance_type": "scheduled",
            "relevant_from": NOW.replace(month=10, day=28),
            "date_precision": "day",
        },
    }
    text = format_item(item)
    assert text.startswith("🏦 Ожидания по ставке ФРС · <b>ставки в целом ▲</b>")
    assert "Заседание 28.10: повышение на 25 б.п. 55.4% → 70.9%" in text
    assert "Когда: событие · 28.10.2026" in text


def test_format_insider():
    item = {
        "source": "sec_edgar",
        "payload_json": {"summary": "Директор купил акции", "event_type": "insider"},
        "raw_json": {
            "form4": {"roles": ["Director"], "owners": ["DOE JOHN"]},
            "significance": "open-market purchase $7.5M",
        },
        "links": [link(importance=4)],
    }
    text = format_item(item)
    assert text.startswith("💼 Инсайдер")
    assert "покупка на рынке $7.5M · Director DOE JOHN" in text


def test_long_digest_is_split_within_limit():
    items = [
        {
            "id": n,
            "source": "finnhub_market_news",
            "payload_json": {"summary": "Очень длинная суть " * 40, "event_type": "other"},
            "links": [link(importance=4)],
        }
        for n in range(10)
    ]
    messages = format_digest(items)
    assert len(messages) > 1
    assert all(len(text) <= 4096 for _, text in messages)
    assert [i for ids, _ in messages for i in ids] == list(range(10))  # every item exactly once
    assert all(text.startswith("📰 <b>Сводка новостей</b>") for _, text in messages)
