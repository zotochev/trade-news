import json

import httpx
import pytest
import respx
import sqlalchemy as sa

from tests.conftest import NOW
from trade_news.annotation.contract import ItemForAnnotation, LLMUnavailable, QuotaExhausted
from trade_news.db.schema import llm_calls
from trade_news.llm.gemini import URL, GeminiClient, GeminiModel
from trade_news.llm.usage import Usage, quota_day_start

A = GeminiModel(
    "model-a", daily_request_limit=3, rpm_limit=100, price_in_per_mtok=1, price_out_per_mtok=2
)
B = GeminiModel("model-b", daily_request_limit=3, rpm_limit=100)
ITEMS = [ItemForAnnotation(1, "s", NOW, "t1", "b"), ItemForAnnotation(2, "s", NOW, "t2", "b")]


def ok_response(ids=(1, 2)):
    text = json.dumps({"items": [{"id": i, "summary": "x"} for i in ids]})
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 1000,
                "candidatesTokenCount": 400,
                "thoughtsTokenCount": 100,
            },
        },
    )


def quota_429(per: str, delay: str | None = None):
    details = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": f"GenerateRequestsPer{per}PerProjectPerModel-FreeTier"}],
        }
    ]
    if delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    return httpx.Response(
        429, json={"error": {"code": 429, "message": "quota", "details": details}}
    )


@pytest.fixture
def make(engine):
    sleeps = []

    def _make(*models):
        client = GeminiClient(
            api_key="KEY",
            models=list(models or (A, B)),
            usage=Usage(engine),
            http=httpx.Client(),
            now=lambda: NOW,
            sleep=sleeps.append,
        )
        client.sleeps = sleeps
        return client

    return _make


def statuses(engine):
    with engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                sa.select(llm_calls.c.model, llm_calls.c.status).order_by(llm_calls.c.id)
            )
        ]


@respx.mock
def test_success_records_usage_and_splits_cost(engine, make):
    route = respx.post(URL.format(model="model-a")).mock(return_value=ok_response())
    anns = make().annotate_batch(ITEMS)
    assert [a.item_id for a in anns] == [1, 2]
    assert (anns[0].input_tokens, anns[0].output_tokens) == (500, 250)  # thinking counted as output
    assert anns[0].cost_estimate == pytest.approx((1000 * 1 + 500 * 2) / 1e6 / 2)
    body = json.loads(route.calls[0].request.content)
    assert body["generationConfig"]["responseJsonSchema"]["properties"]["items"]
    assert route.calls[0].request.headers["x-goog-api-key"] == "KEY"
    assert statuses(engine) == [("model-a", "ok")]


@respx.mock
def test_per_minute_429_waits_and_retries_same_model(engine, make):
    respx.post(URL.format(model="model-a")).mock(
        side_effect=[quota_429("Minute", "7s"), ok_response()]
    )
    client = make()
    assert len(client.annotate_batch(ITEMS)) == 2
    assert client.sleeps and client.sleeps[0] >= 7
    assert statuses(engine) == [("model-a", "quota_minute"), ("model-a", "ok")]


@respx.mock
def test_per_day_429_falls_back_and_is_remembered(engine, make):
    a = respx.post(URL.format(model="model-a")).mock(return_value=quota_429("Day"))
    respx.post(URL.format(model="model-b")).mock(return_value=ok_response())
    client = make()
    client.annotate_batch(ITEMS)
    client.annotate_batch(ITEMS)
    assert a.call_count == 1  # model-a is skipped for the rest of the quota day
    assert statuses(engine) == [("model-a", "quota_day"), ("model-b", "ok"), ("model-b", "ok")]


@respx.mock
def test_local_daily_limit_then_quota_exhausted(engine, make):
    respx.post(URL.format(model="model-a")).mock(return_value=ok_response())
    client = make(A)
    for _ in range(A.daily_request_limit):
        client.annotate_batch(ITEMS)
    with pytest.raises(QuotaExhausted):
        client.annotate_batch(ITEMS)


@respx.mock
def test_bad_request_surfaces_and_5xx_retries(engine, make):
    respx.post(URL.format(model="model-a")).mock(
        return_value=httpx.Response(400, json={"error": {"message": "Invalid JSON schema"}})
    )
    with pytest.raises(LLMUnavailable, match="Invalid JSON schema"):
        make(A).annotate_batch(ITEMS)
    respx.post(URL.format(model="model-b")).mock(side_effect=[httpx.Response(503), ok_response()])
    assert len(make(B).annotate_batch(ITEMS)) == 2


@respx.mock
def test_unparseable_output_returns_nothing(engine, make):
    bad = ok_response()
    bad_json = bad.json()
    bad_json["candidates"][0]["content"]["parts"][0]["text"] = '{"items": [{"id": 1'
    bad_json["candidates"][0]["finishReason"] = "MAX_TOKENS"
    respx.post(URL.format(model="model-a")).mock(return_value=httpx.Response(200, json=bad_json))
    assert make(A).annotate_batch(ITEMS) == []  # the job then retries / dead-letters the items


def test_quota_day_boundary():
    from datetime import UTC, datetime

    assert quota_day_start(datetime(2026, 9, 23, 7, 59, tzinfo=UTC), 8) == datetime(
        2026, 9, 22, 8, tzinfo=UTC
    )
    assert quota_day_start(datetime(2026, 9, 23, 8, 0, tzinfo=UTC), 8) == datetime(
        2026, 9, 23, 8, tzinfo=UTC
    )
