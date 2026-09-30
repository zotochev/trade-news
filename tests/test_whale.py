"""Whale in the capital: deterministic rules, the second pass, delivery hold, formatting."""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from tests.conftest import NOW, news_spec
from tests.test_annotation import SEC_TICKERS, StubLLM, payload
from trade_news import delivery, views
from trade_news.annotation import assets as ref
from trade_news.annotation import whale
from trade_news.annotation.run import annotate_pending
from trade_news.collectors.base import Batch, RawItem
from trade_news.config import LLMConfig
from trade_news.db.schema import annotations, items
from trade_news.pipeline import ingest
from trade_news.telegram.message import format_digest, format_item


@pytest.mark.parametrize(
    ("w", "expected"),
    [
        ({"whale_kind": "stake", "stake_after_pct": 26, "takeover_threshold_pct": 30}, "strong"),
        ({"whale_kind": "stake", "stake_after_pct": 19.1, "takeover_threshold_pct": 30}, "normal"),
        ({"whale_kind": "bid_revision", "price_per_share": 2.5}, "strong"),
        ({"whale_kind": "insider", "price_per_share": 30, "cluster_count": 2}, "strong"),
        ({"whale_kind": "insider", "price_per_share": 30, "cluster_count": 1}, "normal"),
        ({"whale_kind": "bid_revision", "rumor": True}, "strong"),  # strong rules come first
        ({"whale_kind": "bid", "price_per_share": 2.5, "rumor": True}, "weak"),
        ({"whale_kind": "stake", "stake_after_pct": 12, "denied": True}, "weak"),
        ({"whale_kind": "bid"}, "weak"),  # neither price nor stake
        ({"whale_kind": "stake", "stake_after_pct": 19.85}, "normal"),  # US: no threshold
    ],
)
def test_strength(w, expected):
    assert whale.strength(w) == expected


def test_threshold_and_candidates():
    assert whale.takeover_threshold("be") == 30 and whale.takeover_threshold("GB") == 30
    assert whale.takeover_threshold("AU") == 20 and whale.takeover_threshold("US") is None
    assert whale.takeover_threshold(None) is None
    assert whale.is_candidate(
        "finnhub_market_news", "Siris to Acquire 19.1% Stake in Agfa", "", None
    )
    assert whale.is_candidate("marketaux", "Deal news", "", "m_and_a")
    assert not whale.is_candidate("finnhub_market_news", "Apple beats estimates", "", "earnings")
    assert not whale.is_candidate("sec_edgar", "8-K - Takeover Corp", "", "m_and_a")


def form4_raw(acc, owner, day, shares, price, code="P"):
    return {
        "form": "4",
        "form4": {
            "ticker": "NYAX", "issuer_cik": "0001901279", "issuer_name": "Nayax Ltd.",
            "owners": [owner], "roles": ["Chief Executive Officer"], "planned": False,
            "trades": [{"code": code, "shares": shares, "price": price, "date": day}],
        },
        "significance": "open-market purchase",
    }  # fmt: skip


def add_form4(conn, cfg, acc, raw, at):
    ingest(
        conn,
        news_spec("sec_edgar", title_dedup=False),
        Batch([RawItem(acc, f"Form 4 {acc}", "b", None, at, raw)]),
        cfg,
        at,
    )


def test_insider_threshold_and_cluster(engine, cfg):
    with engine.begin() as conn:
        first = form4_raw("a1", "Ben-Asher Yair", "2026-09-24", 20_000, 30.0)  # $600K
        add_form4(conn, cfg, "a1", first, NOW)
        w = whale.insider(conn, first, NOW)
        assert w["whale_kind"] == "insider" and w["cluster_count"] == 1
        assert w["amount_usd"] == 600_000 and w["strength"] == "normal"
        small = form4_raw("a2", "Ben-Asher Yair", "2026-09-25", 1_000, 30.0)
        assert whale.insider(conn, small, NOW) is None  # $30K: below $500K
        later = NOW + timedelta(days=6)
        second = form4_raw("a3", "Ben-Asher Yair", "2026-09-30-05:00", 20_000, 31.0)
        add_form4(conn, cfg, "a3", second, later)
        w2 = whale.insider(conn, second, later)
        assert w2["cluster_count"] == 2 and w2["strength"] == "strong"
        other = form4_raw("a4", "Someone Else", "2026-09-30", 20_000, 31.0)
        assert whale.insider(conn, other, later)["cluster_count"] == 1  # another person
        fund = form4_raw("a5", "X", "2026-09-30", 50_000, 25.0)
        fund["form4"] |= {"issuer_name": "Blackstone Multi-Strategy Hedge Fund", "ticker": None}
        assert whale.insider(conn, fund, later) is None  # non-traded fund shares


class WhaleLLM(StubLLM):
    """Main annotation as StubLLM; the second pass answers from `whales` by item title."""

    def __init__(self, whales, *main):
        super().__init__(*main)
        self.whales = whales
        self.whale_calls = []

    def generate_json(self, prompt, schema, n_items, prompt_version):
        assert prompt_version == whale.WHALE_VERSION
        assert "whale" in schema["$defs"]["WhaleAnswer"]["properties"]
        ids = [int(line[4:]) for line in prompt.splitlines() if line.startswith("id: ")]
        self.whale_calls.append(ids)
        return "stub-w", {"items": [self.whales(i, prompt) for i in ids]}


def test_second_pass_and_delivery_hold(engine, cfg):
    with engine.begin() as conn:
        ref.upsert_assets(conn, ref.yaml_assets(), "assets.yaml")
        ref.upsert_assets(conn, ref.sec_equities(SEC_TICKERS), "sec")
        raw = [
            RawItem("siris", "Siris to Acquire 19.1% Stake in Agfa-Gevaert", "b", None, NOW, {}),
            RawItem("apple", "Apple beats estimates", "b", None, NOW, {}),
        ]
        ingest(conn, news_spec("n", title_dedup=False), Batch(raw), cfg, NOW)
        ids = dict(conn.execute(sa.select(items.c.source_item_id, items.c.id)).all())
        delivery.save_rules(
            conn, delivery.DeliveryRules(enabled=True, min_importance=1, event_types=[]), NOW
        )

    def answer(item_id, _prompt):
        return {
            "id": item_id, "whale": True, "target_listed": True, "new_step": True,
            "whale_kind": "stake", "buyer": "Siris Capital",
            "buyer_type": "private_equity", "target_ticker": "AGFB",
            "exchange": "Euronext Brussels",
            "listing_country": "BE", "stake_before_pct": None, "stake_after_pct": 19.1,
            "price_per_share": 1.2, "currency": "EUR", "conditions": "одобрение <регулятора>",
            "rumor": False, "denied": False,
        }  # fmt: skip

    at = NOW + timedelta(minutes=1)
    # a provider that fails the second pass: the candidate stays pending
    llm = WhaleLLM(lambda i, p: (_ for _ in ()).throw(whale.LLMUnavailable("down")))
    annotate_pending(engine, llm, LLMConfig(batch_size=5), at)
    with engine.connect() as conn:
        pending = conn.execute(sa.select(annotations.c.item_id).where(whale.PENDING)).scalars()
        assert set(pending) == {ids["siris"]}  # "Apple beats estimates" is no candidate
        rules = delivery.load_rules(conn)
        held, _, _ = delivery.matching_items(conn, rules, NOW, now=at + timedelta(minutes=2))
        assert held == [ids["apple"]]  # waits for its whale formatting …
        late, _, _ = delivery.matching_items(conn, rules, NOW, now=at + timedelta(minutes=11))
        assert set(late) == set(ids.values())  # … but not forever

    llm = WhaleLLM(answer)
    whale.resolve_pending(engine, llm)
    assert llm.whale_calls == [[ids["siris"]]]
    with engine.connect() as conn:
        item = views.news_item(conn, ids["siris"])
        assert not list(conn.execute(sa.select(annotations.c.id).where(whale.PENDING)))
        assert views.news_item(conn, ids["apple"])["payload_json"].get("whale") is None
    w = item["payload_json"]["whale"]
    assert w["takeover_threshold_pct"] == 30 and w["strength"] == "normal"
    with engine.connect() as conn:  # a whale skips the importance / direction thresholds
        strict = delivery.DeliveryRules(enabled=True, min_importance=5, require_direction=True)
        passed, _, priority = delivery.matching_items(conn, strict, NOW, now=at)
    assert passed == [ids["siris"]] and priority == {ids["siris"]}
    text = format_item(item)
    assert text.startswith("🟨🟨🟨\n<b>🐋 КИТ В КАПИТАЛЕ</b> · AGFB (Euronext Brussels)\n")
    assert "Доля — → 19.1% · порог 30% · цена 1.2 EUR" in text
    assert "Условия: одобрение &lt;регулятора&gt;" in text  # escaped for HTML parse mode
    assert "⚠️" not in text and "🧊" not in text  # no price / cap data: no badges


def test_private_target_or_speculation_is_no_whale():
    base = {"id": 1, "whale": True, "whale_kind": "bid", "price_per_share": 2.0}
    for flags in ({"target_listed": False, "new_step": True},
                  {"target_listed": True, "new_step": False}):  # fmt: skip
        assert whale.from_answer(whale.WhaleAnswer(**base, **flags), "m")["whale"] is False
    ok = whale.from_answer(whale.WhaleAnswer(**base, target_listed=True, new_step=True), "m")
    assert ok["whale"] and ok["strength"] == "normal"


def item_with(w, summary="Суть"):
    return {
        "id": 1,
        "source": "finnhub_market_news",
        "payload_json": {"summary": summary, "whale": {"whale": True, "version": "x", **w}},
        "links": [payload(1)["assets"][0] | {"symbol": "NYAX"}],
        "raw_url": "https://x.com/a",
    }


def test_format_kinds_and_badges():
    insider = item_with({
        "whale_kind": "insider", "strength": "strong", "cluster_count": 2, "amount_usd": 620_000,
        "buyer": "Ben-Asher Yair", "buyer_roles": "Chief Executive Officer", "currency": "USD",
        "price_per_share": 31.0, "target_ticker": "NYAX",
    })  # fmt: skip
    text = format_item(insider)
    assert text.startswith("🟩🟩🟩\n<b>🐋👤×2 КИТ · ИНСАЙДЕР</b> · NYAX\nСуть\n")
    assert "покупка на рынке $620K · Chief Executive Officer Ben-Asher Yair · цена 31 USD" in text
    revision = item_with({
        "whale_kind": "bid_revision", "strength": "weak", "rumor": True, "target_ticker": "NST",
        "exchange": "ASX", "takeover_threshold_pct": 20, "market_cap_usd": 250e6,
        "price_change_pct": 12.5,
    })  # fmt: skip
    text = format_item(revision)
    assert text.startswith("⬜⬜⬜\n<b>🐋🔁 КИТ · ТОРГ</b> · NST (ASX) · $250 млн ⚠️🧊\n")
    ((_, digest),) = format_digest(
        [revision, item_with({"whale_kind": "bid", "strength": "normal"})]
    )
    assert "<b>🐋🔁 КИТ · ТОРГ</b>" in digest and "<b>🐋🎯 КИТ · ОФЕРТА</b> · NYAX" in digest
    plain = format_item(item_with({}) | {"payload_json": {"summary": "Обычная"}})
    assert "КИТ" not in plain and "🟩" not in plain


def test_competing_bidder_becomes_bid_revision(engine, cfg):
    with engine.begin() as conn:
        ref.upsert_assets(conn, ref.yaml_assets(), "assets.yaml")
        ref.upsert_assets(conn, ref.sec_equities(SEC_TICKERS), "sec")
        raw = [RawItem(k, f"{k} bids for Apple", "b", None, NOW + timedelta(days=d), {})
               for k, d in (("mdp", 0), ("simplify", 3))]  # fmt: skip
        ingest(conn, news_spec("n", title_dedup=False), Batch(raw), cfg, NOW)
        ids = dict(conn.execute(sa.select(items.c.source_item_id, items.c.id)).all())
    annotate_pending(engine, StubLLM(), LLMConfig(batch_size=5), NOW + timedelta(minutes=1))
    with engine.begin() as conn:
        first = {"whale": True, "whale_kind": "bid", "buyer": "Madison Dearborn Partners"}
        aid = conn.execute(
            sa.select(annotations.c.id).where(annotations.c.item_id == ids["mdp"])
        ).scalar()
        whale._store(conn, aid, first)
        at = NOW + timedelta(days=3)
        same = {
            "whale": True,
            "whale_kind": "bid",
            "buyer": "Madison Dearborn",
            "price_per_share": 2,
        }
        whale.apply_competing(conn, ids["simplify"], same, at)
        assert same["whale_kind"] == "bid"  # the same bidder again is no rival
        rival = {"whale": True, "whale_kind": "bid", "buyer": "Simplify Asset Management"}
        whale.apply_competing(conn, ids["simplify"], rival, at)
        assert rival["whale_kind"] == "bid_revision" and rival["strength"] == "strong"
