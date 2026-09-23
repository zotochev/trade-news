import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.conftest import FIXTURES, fake_ctx
from trade_news.collectors.finnhub import fetch_company_news, fetch_market_news, to_raw_item

NEWS = json.loads((FIXTURES / "finnhub_news.json").read_text(encoding="utf-8"))


def test_to_raw_item():
    it = to_raw_item(NEWS[0])
    assert it.source_item_id == str(NEWS[0]["id"])
    assert it.title == NEWS[0]["headline"]
    assert it.published_at == datetime.fromtimestamp(NEWS[0]["datetime"], UTC)
    assert it.raw["related"] == NEWS[0]["related"]


def test_market_news_uses_min_id_cursor():
    seen = []

    def get(url, params, headers):
        seen.append((url, params))
        assert headers["X-Finnhub-Token"] == "k"
        return SimpleNamespace(json=lambda: NEWS if params["category"] == "general" else [])

    ctx = fake_ctx(
        get=get, params={"categories": ["general", "merger"]}, secrets={"FINNHUB_API_KEY": "k"}
    )
    batch = fetch_market_news(ctx, {"general": 5})
    assert seen[0][1]["minId"] == 5
    assert batch.cursor == {"general": max(n["id"] for n in NEWS)}  # merger untouched: no news
    assert len(batch.items) == len(NEWS)


def test_company_news_date_range():
    seen = []

    def get(url, params, headers):
        seen.append(params)
        return SimpleNamespace(json=lambda: NEWS[:1])

    ctx = fake_ctx(
        get=get,
        params={"symbols": ["AAPL", "MSFT"], "lookback_days": 2},
        secrets={"FINNHUB_API_KEY": "k"},
    )
    batch = fetch_company_news(ctx, None)
    assert seen == [
        {"symbol": "AAPL", "from": "2026-09-21", "to": "2026-09-23"},
        {"symbol": "MSFT", "from": "2026-09-21", "to": "2026-09-23"},
    ]
    assert len(batch.items) == 2


def test_error_payload_raises():
    ctx = fake_ctx(
        get=lambda url, **kw: SimpleNamespace(json=lambda: {"error": "Invalid API key"}),
        secrets={"FINNHUB_API_KEY": "k"},
    )
    with pytest.raises(ValueError, match="Invalid API key"):
        fetch_market_news(ctx, None)
