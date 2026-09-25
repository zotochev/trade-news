from datetime import UTC, datetime
from types import SimpleNamespace

from tests.conftest import FIXTURES, fake_ctx
from trade_news.collectors.central_banks import fetch_boe, fetch_fed
from trade_news.collectors.rss import parse_rss

FED = (FIXTURES / "cb_fed_press.xml").read_bytes()  # starts with a BOM, CDATA everywhere
BOE = (FIXTURES / "cb_boe_news.xml").read_bytes()  # guid is not a URL


def test_parse_rss_fed_bom_and_cdata():
    entries = parse_rss(FED)
    assert len(entries) == 3
    e = entries[0]
    assert e["title"].startswith("Federal Reserve Board requests public comment")
    assert e["url"] == "https://www.federalreserve.gov/newsevents/pressreleases/bcreg20260924a.htm"
    assert e["guid"] == e["url"]
    assert e["category"] == "Banking and Consumer Regulatory Policy"
    assert e["pub_date"] == "Thu, 24 Sep 2026 18:30:00 GMT"


def test_fed_description_equal_to_title_is_dropped():
    ctx = fake_ctx(get=lambda url, **kw: SimpleNamespace(content=FED))
    batch = fetch_fed(ctx, None)
    # two default feeds (press releases, speeches), both answered with the same fixture here
    assert len(batch.items) == 6
    it = batch.items[0]
    assert it.body is None
    assert it.published_at == datetime(2026, 9, 24, 18, 30, tzinfo=UTC)
    assert it.raw["hint"] == "US Federal Reserve (currency USD)"
    assert "llm_skip" not in it.raw


def test_old_entries_are_stored_but_skipped_for_llm():
    seen = []

    def get(url, **kw):
        seen.append(url)
        return SimpleNamespace(content=BOE)

    # NOW in tests is 2026-09-23 18:00 UTC -> cutoff 2026-09-22 18:00 UTC
    ctx = fake_ctx(get=get, params={"max_age_hours": 24})
    items = fetch_boe(ctx, None).items
    assert seen == ["https://www.bankofengland.co.uk/rss/news"]
    assert items[0].source_item_id == "{06A73F3E-DFBE-45CC-A9DA-BD7AE031854F}"
    assert [bool(it.raw.get("llm_skip")) for it in items] == [False, True, True]
    assert items[0].body.startswith("The Market Participants Group (MPG)")


def test_feeds_param_overrides_defaults():
    seen = []

    def get(url, **kw):
        seen.append(url)
        return SimpleNamespace(content=BOE)

    fetch_boe(fake_ctx(get=get, params={"feeds": ["https://example.org/a.xml"]}), None)
    assert seen == ["https://example.org/a.xml"]
