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


class Config(BaseModel):
    database_url: str = "sqlite:///data/trade_news.db"
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    rate_limits: dict[str, RateLimit] = Field(default_factory=dict)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("TRADE_NEWS_CONFIG", "config.yaml"))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = Config.model_validate(data or {})
    if url := os.environ.get("DATABASE_URL"):
        cfg.database_url = url
    return cfg
