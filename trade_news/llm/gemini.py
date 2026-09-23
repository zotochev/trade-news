"""Gemini implementation of LLMClient (REST generateContent, no SDK).

Ported from ict-monitor's gemini_client/quota_tracker, with changes:
- models are tried in config order; local RPD/RPM limits are counted from llm_calls (DB),
  so they survive restarts;
- 429 is classified: per-minute → wait (RetryInfo.retryDelay or exponential backoff with jitter)
  and retry the same model; per-day → mark the model exhausted for the quota day, next model;
- structured output via generationConfig.responseJsonSchema (plain JSON Schema generated from
  the contract's pydantic models).

Docs: https://ai.google.dev/gemini-api/docs/structured-output,
https://ai.google.dev/gemini-api/docs/rate-limits
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import structlog

from trade_news.annotation.contract import (
    PROMPT_VERSION,
    Annotation,
    ItemForAnnotation,
    LLMUnavailable,
    QuotaExhausted,
    build_prompt,
    response_json_schema,
)
from trade_news.llm.usage import CallRecord, Usage

log = structlog.get_logger()

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


@dataclass(frozen=True, slots=True)
class GeminiModel:
    name: str
    daily_request_limit: int
    rpm_limit: int
    price_in_per_mtok: float = 0.0  # paid-tier USD per 1M tokens, for cost estimates
    price_out_per_mtok: float = 0.0  # output price includes thinking tokens


@dataclass(frozen=True, slots=True)
class _Outcome:
    data: dict[str, Any] | None  # parsed JSON body of a successful response
    model_done_for_today: bool = False


@dataclass
class GeminiClient:
    api_key: str
    models: list[GeminiModel]
    usage: Usage
    http: httpx.Client
    now: Callable[[], datetime]
    body_max_chars: int = 1500
    max_attempts: int = 4
    timeout: float = 120
    sleep: Callable[[float], None] = field(default=time.sleep)
    provider: str = "gemini"

    def annotate_batch(self, items: list[ItemForAnnotation]) -> list[Annotation]:
        prompt = build_prompt(items, self.body_max_chars)
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": response_json_schema(),
            },
        }
        any_available = False
        for model in self.models:
            if not self._has_daily_quota(model):
                continue
            any_available = True
            self._wait_for_rpm(model)
            outcome = self._call(model, body, len(items))
            if outcome.data is not None:
                return self._to_annotations(model, outcome.data, items)
        if not any_available:
            raise QuotaExhausted("all Gemini models are out of daily quota")
        raise LLMUnavailable("no Gemini model produced a response")

    # --- quota -----------------------------------------------------------------------

    def _has_daily_quota(self, model: GeminiModel) -> bool:
        now = self.now()
        if self.usage.exhausted_today(model.name, now):
            return False
        return self.usage.calls_today(model.name, now) < model.daily_request_limit

    def _wait_for_rpm(self, model: GeminiModel, max_wait: float = 65) -> None:
        waited = 0.0
        while self.usage.calls_last_minute(model.name, self.now()) >= model.rpm_limit:
            if waited >= max_wait:
                return
            self.sleep(5)
            waited += 5

    # --- one model ---------------------------------------------------------------------

    def _call(self, model: GeminiModel, body: dict, n_items: int) -> _Outcome:
        for attempt in range(1, self.max_attempts + 1):
            started = self.now()
            try:
                resp = self.http.post(
                    URL.format(model=model.name),
                    headers={"x-goog-api-key": self.api_key},
                    json=body,
                    timeout=self.timeout,
                )
            except httpx.TransportError as exc:
                self._record(model, started, "error", n_items, error=type(exc).__name__)
                self._backoff(attempt)
                continue

            if resp.status_code == 200:
                data = resp.json()
                in_tok, out_tok = _tokens(data)
                self._record(
                    model, started, "ok", n_items, in_tok, out_tok, _cost(model, in_tok, out_tok)
                )
                return _Outcome(data)

            err = _error(resp)
            if resp.status_code == 429:
                if _is_daily_quota(err):
                    self._record(model, started, "quota_day", n_items, error=_msg(err))
                    log.warning("gemini_daily_quota_exhausted", model=model.name)
                    return _Outcome(None, model_done_for_today=True)
                self._record(model, started, "quota_minute", n_items, error=_msg(err))
                delay = _retry_delay(err)
                log.warning("gemini_rate_limited", model=model.name, retry_in=delay)
                self._backoff(attempt, at_least=delay)
                continue
            if resp.status_code >= 500:
                self._record(
                    model, started, "error", n_items, error=f"{resp.status_code} {_msg(err)}"
                )
                self._backoff(attempt)
                continue
            # 4xx other than 429 is our bug (bad schema, bad key): don't hammer, surface it
            self._record(model, started, "error", n_items, error=f"{resp.status_code} {_msg(err)}")
            raise LLMUnavailable(f"Gemini {model.name}: {resp.status_code} {_msg(err)}")
        return _Outcome(None)

    def _backoff(self, attempt: int, at_least: float | None = None) -> None:
        if attempt >= self.max_attempts:
            return
        delay = random.uniform(0, min(60, 2**attempt))  # exponential with full jitter
        self.sleep(max(delay, at_least or 0))

    def _record(
        self, model, started, status, n_items, in_tok=None, out_tok=None, cost=None, error=None
    ):
        self.usage.record(
            CallRecord(
                provider=self.provider,
                model=model.name,
                started_at=started,
                status=status,
                prompt_version=PROMPT_VERSION,
                n_items=n_items,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_estimate=cost,
                error=error,
            )
        )

    def _to_annotations(
        self, model: GeminiModel, data: dict, items: list[ItemForAnnotation]
    ) -> list[Annotation]:
        in_tok, out_tok = _tokens(data)
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            out_items = parsed["items"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            finish = (data.get("candidates") or [{}])[0].get("finishReason")
            log.warning(
                "gemini_unparseable_response", model=model.name, finish=finish, error=repr(exc)
            )
            return []
        wanted = {it.id for it in items}
        answered = [p for p in out_items if isinstance(p, dict) and p.get("id") in wanted]
        n = max(len(items), 1)  # tokens/cost are per call: split evenly across the batch
        return [
            Annotation(
                item_id=p["id"],
                payload=p,
                model=model.name,
                input_tokens=in_tok // n,
                output_tokens=out_tok // n,
                cost_estimate=_cost(model, in_tok, out_tok) / n,
            )
            for p in answered
        ]


def _tokens(data: dict) -> tuple[int, int]:
    u = data.get("usageMetadata") or {}
    out = (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0)
    return u.get("promptTokenCount") or 0, out


def _cost(model: GeminiModel, in_tok: int, out_tok: int) -> float:
    return round(
        in_tok / 1e6 * model.price_in_per_mtok + out_tok / 1e6 * model.price_out_per_mtok, 6
    )


def _error(resp: httpx.Response) -> dict:
    try:
        return resp.json().get("error") or {}
    except ValueError:
        return {"message": resp.text[:300]}


def _msg(err: dict) -> str:
    return str(err.get("message", ""))[:300]


def _is_daily_quota(err: dict) -> bool:
    for d in err.get("details") or []:
        for v in d.get("violations") or []:
            if "PerDay" in str(v.get("quotaId", "")):
                return True
    return "per day" in _msg(err).lower()


def _retry_delay(err: dict) -> float | None:
    for d in err.get("details") or []:
        if str(d.get("@type", "")).endswith("RetryInfo") and (
            m := re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
        ):
            return float(m.group(1))
    return None
