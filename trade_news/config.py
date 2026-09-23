"""Configuration: non-secret settings from config.yaml, secrets from environment only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class RateLimit(BaseModel):
    calls: int = Field(gt=0)
    period: float = Field(gt=0, description="seconds")


class SourceConfig(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=300, gt=0)
    rate_limit: str | None = None  # key in Config.rate_limits; default = source name
    params: dict[str, Any] = Field(default_factory=dict)


class DedupConfig(BaseModel):
    window_hours: float = 6
    fuzzy_threshold: float = Field(default=90, ge=0, le=100)
    min_title_len: int = 20


class LLMModel(BaseModel):
    name: str
    daily_request_limit: int = Field(gt=0)
    rpm_limit: int = Field(gt=0)
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0


class LLMExclude(BaseModel):
    source: str
    title_regex: str | None = None  # None: the whole source


class LLMConfig(BaseModel):
    provider: str = "gemini"
    batch_size: int = Field(default=25, gt=0)
    interval_seconds: int = Field(default=120, gt=0)
    max_batches_per_run: int = Field(default=10, gt=0)
    max_item_age_hours: float = Field(default=6, gt=0)  # older items are never annotated
    body_max_chars: int = 1500
    quota_reset_hour_utc: int = Field(default=8, ge=0, le=23)
    exclude: list[LLMExclude] = Field(default_factory=list)
    models: list[LLMModel] = Field(default_factory=list)


class RetentionConfig(BaseModel):
    unannotated_days: float = Field(default=30, gt=0)
    annotated_days: float = Field(default=365, gt=0)
    logs_days: float = Field(default=90, gt=0)
    interval_hours: float = Field(default=24, gt=0)


class Config(BaseModel):
    database_url: str = "sqlite:///data/trade_news.db"
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    rate_limits: dict[str, RateLimit] = Field(default_factory=dict)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("TRADE_NEWS_CONFIG", "config.yaml"))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = Config.model_validate(data or {})
    if url := os.environ.get("DATABASE_URL"):
        cfg.database_url = url
    return cfg
