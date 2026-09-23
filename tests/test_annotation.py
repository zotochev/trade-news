"""Stage 3: contract validation, relative dates, asset resolution, the annotation job."""

from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa

from tests.conftest import NOW, news_spec
from trade_news.annotation import assets as ref
from trade_news.annotation.contract import (
    PROMPT_VERSION,
    Annotation,
    ItemForAnnotation,
    LLMUnavailable,
    QuotaExhausted,
    Relevance,
    build_prompt,
    response_json_schema,
    validate_item,
)
from trade_news.annotation.dates import resolve_relevance, rule_dates
from trade_news.annotation.run import annotate_pending
from trade_news.collectors.base import Batch, RawItem
from trade_news.config import LLMConfig, LLMExclude
from trade_news.db.schema import (
    annotation_dead_letters,
    annotations,
    asset_resolution_queue,
    assets,
    item_assets,
    item_relevance,
)
from trade_news.pipeline import ingest

WED = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)  # a Wednesday

SEC_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "2": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
    "3": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    "4": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}


def payload(item_id, **over):
    base = {
        "id": item_id,
        "assets": [
            {
                "asset_class": "equity",
                "scope": "specific",
                "symbol_or_name": "Apple",
                "group_label": None,
                "direction": "bullish",
                "importance": 4,
                "is_primary": True,
                "confidence": 0.9,
            },
            {
                "asset_class": "equity",
                "scope": "market_wide",
                "symbol_or_name": None,
                "group_label": None,
                "direction": "neutral",
                "importance": 2,
                "is_primary": False,
                "confidence": 0.5,
            },
        ],
        "relevance": {
            "type": "immediate",
            "date_iso": None,
            "date_to_iso": None,
            "date_precision": "exact",
            "raw_phrase": None,
        },
        "event_type": "earnings",
        "summary": "Apple отчиталась лучше ожиданий",
    }
    return base | over


# --- contract ---------------------------------------------------------------------


def test_contract_validation():
    assert validate_item(payload(1)).assets[0].symbol_or_name == "Apple"
    bad = payload(1)
    bad["assets"][0]["importance"] = 7
    assert "importance" in validate_item(bad)
    bad = payload(1)
    bad["assets"][0]["symbol_or_name"] = None
    assert "requires symbol_or_name" in validate_item(bad)
    assert "event_type" in validate_item(payload(1, event_type="gossip"))


def test_schema_and_prompt_carry_the_contract():
    schema = response_json_schema()
    assert schema["properties"]["items"]["type"] == "array"
    item = ItemForAnnotation(7, "finnhub", WED, "Fed next Thursday", "body", hints="related: AAPL")
    prompt = build_prompt([item])
    assert "published_at: 2026-09-23T10:00:00+00:00 (Wednesday)" in prompt
    assert "hints: related: AAPL" in prompt and "id: 7" in prompt


# --- relative dates ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "expected", "ambiguous"),
    [
        ("tomorrow", [date(2026, 9, 24)], False),
        ("завтра", [date(2026, 9, 24)], False),
        ("next Wednesday", [date(2026, 9, 30)], False),
        ("next Thursday", [date(2026, 9, 24), date(2026, 10, 1)], True),
        ("в следующий четверг", [date(2026, 9, 24), date(2026, 10, 1)], True),
        ("on Friday", [date(2026, 9, 25)], False),
        ("в пятницу", [date(2026, 9, 25)], False),
        ("at the end of October", None, None),
    ],
)
def test_rule_dates(phrase, expected, ambiguous):
    reading = rule_dates(phrase, WED)
    if expected is None:
        assert reading is None
    else:
        assert list(reading.candidates) == expected and reading.ambiguous == ambiguous


def rel(type_, date_iso=None, precision="day", phrase=None, date_to=None):
    return Relevance(
        type=type_,
        date_iso=date_iso,
        date_to_iso=date_to,
        date_precision=precision,
        raw_phrase=phrase,
    )


def test_llm_resolving_from_today_is_corrected_by_rule():
    # the model returned published_at itself for "next Wednesday" (seen live)
    row = resolve_relevance(rel("scheduled", "2026-09-23", phrase="next Wednesday"), WED)
    assert row["relevant_from"] == datetime(2026, 9, 30, tzinfo=UTC)
    assert (row["resolved_by"], row["needs_review"]) == ("rule", False)


def test_ambiguous_phrase_keeps_llm_reading_if_acceptable():
    row = resolve_relevance(rel("scheduled", "2026-10-01", phrase="next Thursday"), WED)
    assert row["relevant_from"].date() == date(2026, 10, 1) and row["resolved_by"] == "llm"
    fixed = resolve_relevance(rel("scheduled", "2026-11-05", phrase="next Thursday"), WED)
    assert fixed["relevant_from"].date() == date(2026, 9, 24) and fixed["needs_review"]


def test_dates_outside_window_become_unknown():
    for bad in ("2026-09-21", "2028-06-01", "garbage"):
        row = resolve_relevance(rel("scheduled", bad), WED)
        assert row["relevance_type"] == "unknown" and row["relevant_from"] is None
        assert row["needs_review"]
    ok = resolve_relevance(rel("scheduled", "2026-09-22T20:00:00Z", precision="exact"), WED)
    assert ok["relevance_type"] == "scheduled"  # -1 day is still inside the window


def test_immediate_window_and_precision():
    imm = resolve_relevance(rel("immediate"), WED)
    assert imm["relevant_from"] == WED and imm["relevant_to"] is None
    q = resolve_relevance(rel("window", "2027-01-01", precision="quarter"), WED)
    assert q["relevant_to"].date() == date(2027, 3, 31)
    m = resolve_relevance(rel("window", "2026-10", precision="month"), WED)
    assert (m["relevant_from"].date(), m["relevant_to"].date()) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert (
        resolve_relevance(rel("scheduled", "2026-10-28", precision="exact"), WED)["date_precision"]
        == "day"
    )


# --- assets -----------------------------------------------------------------------


@pytest.fixture
def seeded(engine):
    with engine.begin() as conn:
        ref.upsert_assets(conn, ref.yaml_assets(), "assets.yaml")  # before SEC on purpose
        ref.upsert_assets(conn, ref.sec_equities(SEC_TICKERS), "sec")
    return engine


def test_norm():
    assert ref.norm("Apple Inc.") == "apple"
    assert ref.norm("MICROSOFT CORP") == "microsoft"
    assert ref.norm("The Home Depot, Inc.") == "home depot"
    assert ref.norm("Биткоин") == "биткоин"
    assert ref.compact("EUR/USD") == "EURUSD" and ref.compact("BRK.B") == "BRKB"


@pytest.mark.parametrize(
    ("cls", "text", "symbol"),
    [
        ("equity", "AAPL", "AAPL"),
        ("equity", "Apple Inc", "AAPL"),
        ("equity", "эпл", "AAPL"),
        ("equity", "Alphabet", "GOOGL"),  # primary share class gets the name
        ("equity", "Google", "GOOGL"),
        ("equity", "GOOG", "GOOG"),
        ("equity", "BRK.B", "BRK-B"),
        ("equity", "Microsoft", "MSFT"),
        ("fx", "EUR/USD", "EURUSD"),
        ("fx", "евродоллар", "EURUSD"),
        ("crypto", "биткоин", "BTC"),
        ("index", "S&P 500", "SPX"),
        ("rates", "10-year Treasury", "US10Y"),
        ("equity", "Bitcoin", None),  # right name, wrong class: not guessed
        ("equity", "Sierra Space", None),
    ],
)
def test_resolve(seeded, cls, text, symbol):
    with seeded.connect() as conn:
        asset_id = ref.resolve(conn, cls, text)
        got = conn.execute(sa.select(assets.c.symbol).where(assets.c.id == asset_id)).scalar()
    assert got == symbol


def test_seed_is_idempotent_and_backfills_cik(seeded):
    with seeded.begin() as conn:
        assert ref.upsert_assets(conn, ref.yaml_assets(), "assets.yaml") == (0, 0)
        assert ref.upsert_assets(conn, ref.sec_equities(SEC_TICKERS), "sec") == (0, 0)
        # AAPL was created by assets.yaml first, yet has the CIK from SEC
        assert ref.equity_by_cik(conn, "320193") == "AAPL"


# --- the job ---------------------------------------------------------------------


class StubLLM:
    """Scripted LLMClient: `responses` is consumed call by call; a callable builds payloads."""

    provider = "stub"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[list[int]] = []

    def annotate_batch(self, items):
        self.calls.append([it.id for it in items])
        r = self.responses.pop(0) if self.responses else None
        if r is None:
            r = lambda its: [payload(i.id) for i in its]  # noqa: E731
        if isinstance(r, Exception):
            raise r
        payloads = r(items) if callable(r) else r
        return [Annotation(p["id"], p, "stub-1", 100, 50, 0.001) for p in payloads]


def add_items(
    engine, cfg, n, source="news", title="Apple beats estimates, raises guidance", fetched=NOW
):
    spec = news_spec(source, title_dedup=False)
    raw = [RawItem(f"{source}-{i}", f"{title} #{i}", "body", None, fetched, {}) for i in range(n)]
    with engine.begin() as conn:
        ingest(conn, spec, Batch(raw), cfg, fetched_at=fetched)


def count(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(table).where(*where)).scalar()


LLM = LLMConfig(batch_size=2, max_batches_per_run=5)


def test_annotates_and_is_idempotent(seeded, cfg):
    add_items(seeded, cfg, 3)
    llm = StubLLM()
    stats = annotate_pending(seeded, llm, LLM, NOW + timedelta(minutes=1))
    assert (stats.batches, stats.annotated, stats.unresolved_links) == (2, 3, 0)
    assert count(seeded, annotations) == 3
    assert count(seeded, item_assets) == 6 and count(seeded, item_relevance) == 3
    with seeded.connect() as conn:
        linked = conn.execute(
            sa.select(assets.c.symbol).join(item_assets, item_assets.c.asset_id == assets.c.id)
        ).scalars()
        assert set(linked) == {"AAPL"}
    again = annotate_pending(seeded, llm, LLM, NOW + timedelta(minutes=2))
    assert again.batches == 0 and len(llm.calls) == 2


def test_invalid_answer_retried_once_then_dead_letter(seeded, cfg):
    add_items(seeded, cfg, 2)
    bad = lambda its: [payload(i.id, event_type="gossip") for i in its]  # noqa: E731
    llm = StubLLM(
        lambda its: [payload(its[0].id), payload(its[1].id, event_type="gossip")],
        bad,  # the retry of the second item fails again
    )
    stats = annotate_pending(seeded, llm, LLM, NOW)
    assert (stats.annotated, stats.retried, stats.dead_lettered) == (1, 1, 1)
    assert len(llm.calls[1]) == 1  # only the failed item was re-sent
    with seeded.connect() as conn:
        err = conn.execute(sa.select(annotation_dead_letters.c.error)).scalar()
    assert "event_type" in err
    assert annotate_pending(seeded, StubLLM(), LLM, NOW).batches == 0  # not picked again


def test_missing_items_are_retried(seeded, cfg):
    add_items(seeded, cfg, 2)
    llm = StubLLM(lambda its: [payload(its[0].id)])  # the model skipped one item
    stats = annotate_pending(seeded, llm, LLM, NOW)
    assert (stats.annotated, stats.retried, stats.dead_lettered) == (2, 1, 0)


def test_quota_exhausted_leaves_items_pending(seeded, cfg):
    add_items(seeded, cfg, 4)
    stats = annotate_pending(seeded, StubLLM(None, QuotaExhausted()), LLM, NOW)
    assert stats.stopped == "quota" and stats.annotated == 2
    stats = annotate_pending(seeded, StubLLM(LLMUnavailable("503")), LLM, NOW)
    assert stats.stopped == "unavailable"
    assert annotate_pending(seeded, StubLLM(), LLM, NOW).annotated == 2  # picked up later


def test_unresolved_specific_asset_is_queued_not_dropped(seeded, cfg):
    add_items(seeded, cfg, 1)
    p = payload(0)
    llm = StubLLM(
        lambda its: [
            payload(its[0].id, assets=[p["assets"][0] | {"symbol_or_name": "Sierra Space"}])
        ]
    )
    stats = annotate_pending(seeded, llm, LLM, NOW)
    assert stats.unresolved_links == 1
    with seeded.connect() as conn:
        assert (
            conn.execute(sa.select(asset_resolution_queue.c.symbol_or_name)).scalar()
            == "Sierra Space"
        )
        row = conn.execute(sa.select(item_assets)).one()
    assert row.asset_id is None and row.raw_symbol == "Sierra Space"


def test_filters_age_exclusions_and_dedup_members(seeded, cfg):
    add_items(seeded, cfg, 1, source="old", fetched=NOW - timedelta(hours=7))
    add_items(seeded, cfg, 2, source="sec_edgar", title="4 - Some Insider (0000000001) (Issuer)")
    add_items(seeded, cfg, 1, source="sec_edgar", title="8-K - Apple Inc. (0000320193) (Filer)")
    llm_cfg = LLM.model_copy(
        update={"exclude": [LLMExclude(source="sec_edgar", title_regex=r"^4(/A)? - ")]}
    )
    llm = StubLLM()
    annotate_pending(seeded, llm, llm_cfg, NOW + timedelta(minutes=1))
    assert [len(c) for c in llm.calls] == [1]  # only the 8-K
    with seeded.connect() as conn:
        pv = conn.execute(sa.select(annotations.c.prompt_version)).scalar()
    assert pv == PROMPT_VERSION
