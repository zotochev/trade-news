"""LLM call log (llm_calls) and local quota checks derived from it.

Counting from the database (instead of in-memory counters) means restarts don't forget how many
requests were already made today. A provider 429 "per day" is still authoritative (quota_day).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa

from trade_news.db.schema import llm_calls


@dataclass(frozen=True, slots=True)
class CallRecord:
    provider: str
    model: str
    started_at: datetime
    status: str  # ok | quota_day | quota_minute | error
    prompt_version: str | None = None
    n_items: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_estimate: float | None = None
    error: str | None = None


def quota_day_start(now: datetime, reset_hour_utc: int) -> datetime:
    """Start of the provider's quota day containing `now`. Gemini resets RPD at midnight
    Pacific; ict-monitor observed it as 08:00 UTC."""
    start = now.replace(hour=reset_hour_utc, minute=0, second=0, microsecond=0)
    return start if now >= start else start - timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Usage:
    engine: sa.Engine
    reset_hour_utc: int = 8

    def record(self, rec: CallRecord) -> None:
        with self.engine.begin() as conn:
            conn.execute(llm_calls.insert().values(**asdict(rec)))

    def calls_today(self, model: str, now: datetime) -> int:
        since = quota_day_start(now, self.reset_hour_utc)
        return self._count(model, since)

    def calls_last_minute(self, model: str, now: datetime) -> int:
        return self._count(model, now - timedelta(seconds=60))

    def exhausted_today(self, model: str, now: datetime) -> bool:
        """The provider itself said the daily quota is gone (authoritative)."""
        since = quota_day_start(now, self.reset_hour_utc)
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    sa.select(sa.func.count()).where(
                        llm_calls.c.model == model,
                        llm_calls.c.status == "quota_day",
                        llm_calls.c.started_at >= since,
                    )
                ).scalar()
            )

    def _count(self, model: str, since: datetime) -> int:
        with self.engine.connect() as conn:
            return conn.execute(
                sa.select(sa.func.count()).where(
                    llm_calls.c.model == model, llm_calls.c.started_at >= since
                )
            ).scalar()
