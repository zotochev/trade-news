import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from tests.conftest import FIXTURES, fake_ctx
from trade_news.collectors.marketaux import fetch, to_raw_item

RESP = json.loads((FIXTURES / "marketaux_news.json").read_text(encoding="utf-8"))
SECRETS = {"MARKETAUX_API_KEY": "secret-token"}


def ok_get(seen):
    def get(url, params):
        seen.append(params)
        return SimpleNamespace(json=lambda: RESP)

    return get


def test_to_raw_item():
    a = RESP["data"][0]
    it = to_raw_item(a)
    assert it.source_item_id == a["uuid"]
    assert it.title == a["title"]
    assert it.body == a["description"]
    assert it.published_at == datetime(2026, 9, 25, 16, 7, 12, tzinfo=UTC)
    assert it.raw["hint"] == "tickers: AMD, NVDA, MU, PAYX"


def test_query_and_budget_counter():
    seen = []
    batch = fetch(fake_ctx(get=ok_get(seen), secrets=SECRETS), None)
    assert len(batch.items) == 2
    assert seen[0]["api_token"] == "secret-token"
    assert seen[0]["countries"] == "us" and seen[0]["must_have_entities"] == "true"
    assert batch.cursor == {"day": "2026-09-23", "calls": 1}


def test_budget_spent_makes_no_request():
    ctx = fake_ctx(params={"daily_request_budget": 5}, secrets=SECRETS)  # any HTTP call fails
    batch = fetch(ctx, {"day": "2026-09-23", "calls": 5})
    assert batch.items == []
    assert batch.cursor == {"day": "2026-09-23", "calls": 5}


def test_budget_resets_on_new_day():
    seen = []
    batch = fetch(fake_ctx(get=ok_get(seen), secrets=SECRETS), {"day": "2026-09-22", "calls": 80})
    assert len(seen) == 1
    assert batch.cursor == {"day": "2026-09-23", "calls": 1}


def test_http_error_hides_token():
    def get(url, params):
        req = httpx.Request("GET", url, params=params)
        resp = httpx.Response(402, request=req, text='{"error":{"code":"usage_limit_reached"}}')
        resp.raise_for_status()

    with pytest.raises(RuntimeError, match="usage_limit_reached") as exc:
        fetch(fake_ctx(get=get, secrets=SECRETS), None)
    assert "secret-token" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
