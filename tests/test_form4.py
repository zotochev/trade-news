from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from tests.conftest import FIXTURES, NOW, fake_ctx, news_spec
from tests.test_annotation import StubLLM, payload
from trade_news import views
from trade_news.annotation.run import annotate_pending, pending_items
from trade_news.collectors import form4
from trade_news.collectors.base import Batch, RawItem
from trade_news.collectors.sec_edgar import fetch
from trade_news.config import LLMConfig
from trade_news.db.schema import annotations
from trade_news.pipeline import ingest


def load(name):
    return form4.parse((FIXTURES / f"{name}.txt").read_text(encoding="utf-8", errors="replace"))


def test_parse_purchase():
    f = load("form4_purchase")
    assert f.ticker == "PNRG" and "Director" in f.roles and f.planned is False
    assert [t.code for t in f.trades] == ["P"]
    assert f.total("P") == pytest.approx(100 * 211.70)
    assert "open-market purchase: 100 shares at avg $211.70 = $21K" in form4.describe(f)
    assert form4.headline(f) == "Form 4 · PNRG · Director buys $21K"


def test_parse_sales_and_plan_flag():
    planned = load("form4_sale_10b5")
    assert planned.ticker == "BLSH" and planned.planned is True  # "true" spelling
    assert planned.total("S") > 5_000_000
    ceo = load("form4_sale")
    assert ceo.ticker == "QSI" and ceo.roles[0] == "President & CEO" and ceo.planned is False


def test_noise_has_no_open_market_trades():
    f = load("form4_rsu")
    assert {t.code for t in f.trades} == {"M"}
    assert form4.significance(f, form4.Thresholds()) == (False, "no open-market trades")
    assert "grants/exercises/withholding" in form4.describe(f)


@pytest.mark.parametrize(
    ("name", "th", "ok", "why"),
    [
        ("form4_purchase", form4.Thresholds(), False, "below thresholds"),
        ("form4_purchase", form4.Thresholds(min_buy_usd=10_000), True, "open-market purchase $21K"),
        ("form4_sale_10b5", form4.Thresholds(), False, "sale under a 10b5-1 plan"),
        ("form4_sale_10b5", form4.Thresholds(skip_planned_sales=False), True, "open-market sale"),
        ("form4_sale", form4.Thresholds(min_sell_usd=10_000), True, "open-market sale $23K"),
    ],
)
def test_significance(name, th, ok, why):
    significant, reason = form4.significance(load(name), th)
    assert significant is ok and reason.startswith(why)


def test_parse_rejects_non_ownership_documents():
    assert form4.parse("<XML><foo/></XML>") is None
    assert form4.parse("not xml at all") is None


def _feed_entry(acc: str, cik: str, updated: str) -> str:
    url = f"https://www.sec.gov/Archives/edgar/data/1/{acc.replace('-', '')}/{acc}-index.htm"
    return f"""<entry><title>4 - Some Issuer ({cik}) (Issuer)</title>
      <link rel="alternate" type="text/html" href="{url}"/>
      <summary type="html">x</summary><updated>{updated}</updated>
      <category scheme="https://www.sec.gov/" label="form type" term="4"/>
      <id>urn:tag:sec.gov,2008:accession-number={acc}</id></entry>"""


def test_collector_enriches_new_form4_only():
    feed = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(
            [
                _feed_entry("0000000001-26-000001", "0000000001", "2026-09-23T15:00:00+00:00"),
                _feed_entry("0000000001-26-000002", "0000000002", "2026-09-23T15:05:00+00:00"),
                _feed_entry(
                    "0000000001-26-000003", "0000000003", "2026-09-23T13:00:00+00:00"
                ),  # seen
            ]
        )
        + "</feed>"
    )
    docs = {
        "0000000001-26-000001.txt": (FIXTURES / "form4_purchase.txt").read_text(encoding="utf-8"),
        "0000000001-26-000002.txt": (FIXTURES / "form4_rsu.txt").read_text(encoding="utf-8"),
    }
    fetched = []

    def get(url, params=None, headers=None):
        if url.endswith(".txt"):
            fetched.append(url.rsplit("/", 1)[1])
            return SimpleNamespace(text=docs[url.rsplit("/", 1)[1]])
        return SimpleNamespace(content=feed.encode())

    ctx = fake_ctx(
        get=get,
        params={"forms": ["4"], "form4_thresholds": {"min_buy_usd": 10_000}},
        secrets={"SEC_USER_AGENT": "a b@c.d"},
    )
    batch = fetch(ctx, {"4": "2026-09-23T14:00:00+00:00"})
    by_id = {it.source_item_id: it for it in batch.items}
    assert sorted(fetched) == sorted(docs)  # the already-seen filing is not downloaded again
    buy = by_id["0000000001-26-000001"]
    assert buy.title == "Form 4 · PNRG · Director buys $21K" and "llm_skip" not in buy.raw
    assert buy.raw["form4"]["ticker"] == "PNRG" and "$211.70" in buy.body
    assert by_id["0000000001-26-000002"].raw["llm_skip"] == "no open-market trades"
    assert by_id["0000000001-26-000003"].raw["llm_skip"] == "form 4 without details"


def test_llm_skip_items_are_not_annotated(engine, cfg):
    items = [
        RawItem("a", "Form 4 · X · CEO buys $2.0M", "facts", None, NOW, {"form": "4"}),
        RawItem(
            "b",
            "Form 4 · Y · Director buys $5K",
            "facts",
            None,
            NOW,
            {"form": "4", "llm_skip": "below"},
        ),
    ]
    with engine.begin() as conn:
        ingest(conn, news_spec("sec_edgar", title_dedup=False), Batch(items), cfg, NOW)
        pending = pending_items(conn, LLMConfig(), NOW, 10)
    assert [p.title for p in pending] == ["Form 4 · X · CEO buys $2.0M"]


def test_readers_use_latest_annotation_only(engine, cfg):
    with engine.begin() as conn:
        ingest(
            conn,
            news_spec("n"),
            Batch([RawItem("1", "Apple beats estimates", "b", None, NOW, {})]),
            cfg,
            NOW,
        )
    annotate_pending(engine, StubLLM(), LLMConfig(), NOW)
    # a newer prompt version re-annotates the same item with a different link
    with engine.begin() as conn:
        conn.execute(annotations.update().values(prompt_version="v0"))
    base = payload(0)["assets"][0]
    annotate_pending(
        engine,
        StubLLM(lambda its: [payload(its[0].id, assets=[base | {"symbol_or_name": "Tesla"}])]),
        LLMConfig(),
        NOW,
    )
    with engine.connect() as conn:
        item_id = conn.execute(sa.select(annotations.c.item_id)).scalar()
        links = views.links_by_item(conn, [item_id])[item_id]
        assert conn.execute(sa.select(sa.func.count()).select_from(annotations)).scalar() == 2
    assert [link["raw_symbol"] for link in links] == ["Tesla"]


def test_same_trade_in_two_filings_is_merged_exactly(engine, cfg):
    spec = news_spec("sec_edgar", title_dedup=False)
    exact = {"form": "4", "dedup_title": "exact"}
    items = [
        RawItem("f1", "Form 4 · ETRA · Director buys $20.0M", "b", "https://sec.gov/1", NOW, exact),
        RawItem("f2", "Form 4 · ETRA · Director buys $20.0M", "b", "https://sec.gov/2", NOW, exact),
        RawItem("f3", "Form 4 · AIAI · Director buys $150K", "b", "https://sec.gov/3", NOW, exact),
        RawItem("f4", "Form 4 · BPRE · Director buys $125K", "b", "https://sec.gov/4", NOW, exact),
    ]
    with engine.begin() as conn:
        stats = ingest(conn, spec, Batch(items), cfg, NOW)
    assert stats.dedup_merged == 1  # only ETRA; similar-looking different trades stay apart


def test_usd_formatting_at_boundaries():
    assert (
        form4._usd(999_999.5) == "$1.0M" and form4._usd(999) == "$999" and form4._usd(1500) == "$2K"
    )
