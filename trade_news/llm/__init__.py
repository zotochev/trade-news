"""LLM providers. The domain only sees annotation.contract.LLMClient; the provider is picked
by `llm.provider` in config.yaml."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime

import httpx
import sqlalchemy as sa

from trade_news.annotation.contract import LLMClient
from trade_news.config import LLMConfig
from trade_news.llm.usage import Usage

SECRETS = {"gemini": "GEMINI_API_KEY"}


def missing_secret(cfg: LLMConfig) -> str | None:
    name = SECRETS.get(cfg.provider)
    return name if name and not os.environ.get(name) else None


def make_client(
    cfg: LLMConfig, engine: sa.Engine, http: httpx.Client, now: Callable[[], datetime]
) -> LLMClient:
    usage = Usage(engine, reset_hour_utc=cfg.quota_reset_hour_utc)
    if cfg.provider == "gemini":
        from trade_news.llm.gemini import GeminiClient, GeminiModel

        return GeminiClient(
            api_key=os.environ["GEMINI_API_KEY"],
            models=[GeminiModel(**m.model_dump()) for m in cfg.models],
            usage=usage,
            http=http,
            now=now,
            body_max_chars=cfg.body_max_chars,
        )
    raise ValueError(f"unknown llm.provider: {cfg.provider!r}")
