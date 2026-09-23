from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog

from trade_news.cli import cmd_db_upgrade
from trade_news.collectors.base import CollectorSpec, Context
from trade_news.config import Config
from trade_news.db import make_engine

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}")


@pytest.fixture
def engine(cfg):
    cmd_db_upgrade(cfg)  # the real migrations, not metadata.create_all
    eng = make_engine(cfg.database_url)
    yield eng
    eng.dispose()


def news_spec(name="news", title_dedup=True, fetch=None) -> CollectorSpec:
    return CollectorSpec(name=name, fetch=fetch or (lambda ctx, cur: None), title_dedup=title_dedup)


def fake_ctx(get=None, params=None, secrets=None, source="test") -> Context:
    return Context(
        source=source,
        get=get or (lambda url, **kw: pytest.fail("unexpected HTTP call")),
        params=params or {},
        secrets=secrets or {},
        now=lambda: NOW,
        log=structlog.get_logger(),
    )
