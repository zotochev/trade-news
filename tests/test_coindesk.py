from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from tests.conftest import FIXTURES, fake_ctx
from trade_news.collectors.coindesk import FEED_URL, fetch

FEED = (FIXTURES / "coindesk.xml").read_bytes()


def test_fetch_items_and_hint():
    seen = []

    def get(url, headers):
        seen.append(url)
        return SimpleNamespace(content=FEED)

    # NOW in tests is 2026-09-23 18:00 UTC, the fixture is from 2026-09-25: nothing is old
    items = fetch(fake_ctx(get=get), None).items
    assert seen == [FEED_URL]
    assert len(items) == 3
    it = items[0]
    assert it.source_item_id == "a0b4a6ba-5569-4fef-940c-e402f9698d89"
    assert it.title.startswith("Circle and Tether step in to freeze hacker wallet")
    assert it.body.startswith("The stablecoin issuer blacklisted a wallet")
    assert it.published_at == datetime(2026, 9, 25, 14, 41, 53, tzinfo=UTC)
    assert it.raw["hint"] == "crypto news; topics: Markets, Circle, Tether"
    assert "llm_skip" not in it.raw


def test_old_entries_skipped_for_llm():
    ctx = fake_ctx(
        get=lambda url, **kw: SimpleNamespace(content=FEED),
        params={"max_age_hours": 24},
    )
    ctx = replace(ctx, now=lambda: datetime(2026, 9, 27, tzinfo=UTC))
    assert all(it.raw["llm_skip"] for it in fetch(ctx, None).items)
