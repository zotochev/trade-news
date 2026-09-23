"""Per-source health from collector_runs: used by the bot's /sources."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa

from trade_news.db.schema import collector_runs as r


@dataclass(frozen=True, slots=True)
class SourceStatus:
    name: str
    description: str
    last_started: datetime | None
    last_status: str | None  # running | ok | error | aborted | None (never ran)
    new_24h: int


def source_statuses(
    conn: sa.Connection, sources: Sequence[tuple[str, str]], now: datetime
) -> list[SourceStatus]:
    """`sources`: (name, description) of the collectors this process runs, in display order."""
    names = [name for name, _ in sources]
    latest_ids = sa.select(sa.func.max(r.c.id)).where(r.c.source.in_(names)).group_by(r.c.source)
    latest = {
        row.source: row
        for row in conn.execute(
            sa.select(r.c.source, r.c.started_at, r.c.status).where(r.c.id.in_(latest_ids))
        )
    }
    new_24h = dict(
        conn.execute(
            sa.select(r.c.source, sa.func.coalesce(sa.func.sum(r.c.inserted), 0))
            .where(r.c.source.in_(names), r.c.started_at >= now - timedelta(hours=24))
            .group_by(r.c.source)
        ).all()
    )
    return [
        SourceStatus(
            name=name,
            description=description or name,
            last_started=latest[name].started_at if name in latest else None,
            last_status=latest[name].status if name in latest else None,
            new_24h=int(new_24h.get(name, 0)),
        )
        for name, description in sources
    ]
