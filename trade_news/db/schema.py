"""Database schema (SQLAlchemy Core).

Portable between SQLite and PostgreSQL:
- enums are stored as text + CHECK constraint (no native PG enum types);
- all timestamps go through `UTCDateTime`, which rejects naive datetimes on write
  and always returns tz-aware UTC on read (SQLite has no timestamptz);
- bigint PKs degrade to INTEGER on SQLite so autoincrement works;
- JSON becomes JSONB on PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

ASSET_CLASSES = ("equity", "fx", "crypto", "commodity", "index", "rates", "macro")
SCOPES = ("market_wide", "group", "specific")
DIRECTIONS = ("bullish", "bearish", "neutral")
RELEVANCE_TYPES = ("immediate", "scheduled", "window", "unknown")
DATE_PRECISIONS = ("exact", "day", "month", "quarter", "unknown")


class UTCDateTime(TypeDecorator):
    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime is not allowed: {value!r}")
        value = value.astimezone(UTC)
        # SQLite stores text; keep it naive-UTC so string comparison stays correct.
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


BigId = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
Json = sa.JSON().with_variant(JSONB(), "postgresql")


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=16)


def _pk() -> sa.Column:
    return sa.Column("id", BigId, primary_key=True, autoincrement=True)


def _now() -> datetime:
    return datetime.now(UTC)


metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

# --- ingestion --------------------------------------------------------------

# Everything as received. A changed revision of the same source item gets its own row.
raw_items = sa.Table(
    "raw_items",
    metadata,
    _pk(),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("source_item_id", sa.Text, nullable=False),
    sa.Column("url", sa.Text),
    sa.Column("title", sa.Text),
    sa.Column("body", sa.Text),
    sa.Column("published_at", UTCDateTime),
    sa.Column("fetched_at", UTCDateTime, nullable=False),
    sa.Column("raw_json", Json, nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.UniqueConstraint("source", "source_item_id", "content_hash"),
)

# One row per (source, source_item_id). Duplicates across sources share dedup_group_id,
# which is the id of the first item of the group.
items = sa.Table(
    "items",
    metadata,
    _pk(),
    sa.Column("raw_item_id", BigId, sa.ForeignKey("raw_items.id"), nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("source_item_id", sa.Text, nullable=False),
    sa.Column("canonical_url", sa.Text),
    sa.Column("title", sa.Text),
    sa.Column("title_norm", sa.Text),
    sa.Column("title_hash", sa.String(64)),
    sa.Column("body", sa.Text),
    sa.Column("published_at", UTCDateTime, nullable=False),
    sa.Column("fetched_at", UTCDateTime, nullable=False),
    sa.Column("dedup_group_id", BigId, index=True),
    sa.Column("dedup_reason", sa.Text),  # url | title_hash | fuzzy | NULL for group leaders
    sa.UniqueConstraint("source", "source_item_id"),
    sa.Index(None, "published_at"),
    sa.Index(None, "canonical_url"),
    sa.Index(None, "title_hash"),
)

collector_state = sa.Table(
    "collector_state",
    metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("cursor", Json),
    sa.Column("updated_at", UTCDateTime, nullable=False, default=_now, onupdate=_now),
)

collector_runs = sa.Table(
    "collector_runs",
    metadata,
    _pk(),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("started_at", UTCDateTime, nullable=False),
    sa.Column("finished_at", UTCDateTime),
    sa.Column("status", sa.String(16), nullable=False),  # running | ok | error
    sa.Column("fetched", sa.Integer, nullable=False, default=0),
    sa.Column("inserted", sa.Integer, nullable=False, default=0),
    sa.Column("seen_before", sa.Integer, nullable=False, default=0),
    sa.Column("dedup_merged", sa.Integer, nullable=False, default=0),
    sa.Column("error", sa.Text),
    sa.Index(None, "source", "started_at"),
)

# --- assets -----------------------------------------------------------------

assets = sa.Table(
    "assets",
    metadata,
    _pk(),
    sa.Column("asset_class", _enum("asset_class", ASSET_CLASSES), nullable=False),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("name", sa.Text),
    sa.Column("exchange", sa.Text),
    sa.Column("base_ccy", sa.Text),
    sa.Column("quote_ccy", sa.Text),
    sa.Column("sector", sa.Text),
    sa.Column("cik", sa.String(10), index=True),  # SEC CIK for equities: exact match for filings
    sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.UniqueConstraint("asset_class", "symbol"),
    sa.Index(None, "symbol"),
)

asset_aliases = sa.Table(
    "asset_aliases",
    metadata,
    _pk(),
    sa.Column("asset_id", BigId, sa.ForeignKey("assets.id"), nullable=False, index=True),
    sa.Column("alias", sa.Text, nullable=False),
    sa.Column("alias_norm", sa.Text, nullable=False, index=True),  # see annotation.assets.norm
    sa.Column("lang", sa.Text),
    sa.Column("source", sa.Text),
    sa.UniqueConstraint("alias", "lang"),
)

# --- annotation -------------------------------------------------------------

item_assets = sa.Table(
    "item_assets",
    metadata,
    _pk(),
    sa.Column("item_id", BigId, sa.ForeignKey("items.id"), nullable=False, index=True),
    sa.Column("annotation_id", BigId, sa.ForeignKey("annotations.id")),
    sa.Column("asset_class", _enum("asset_class", ASSET_CLASSES), nullable=False),
    sa.Column("scope", _enum("scope", SCOPES), nullable=False),
    sa.Column("asset_id", BigId, sa.ForeignKey("assets.id")),
    sa.Column("raw_symbol", sa.Text),  # symbol_or_name as the LLM returned it
    sa.Column("group_label", sa.Text),
    sa.Column("direction", _enum("direction", DIRECTIONS)),
    sa.Column("importance", sa.SmallInteger),
    sa.Column("confidence", sa.Numeric(3, 2, asdecimal=False)),
    sa.Column("is_primary", sa.Boolean),
    sa.CheckConstraint("importance between 1 and 5", name="importance_range"),
    sa.CheckConstraint("confidence between 0 and 1", name="confidence_range"),
    sa.Index(None, "asset_class", "scope"),
    sa.Index(
        None,
        "asset_id",
        sqlite_where=sa.text("asset_id is not null"),
        postgresql_where=sa.text("asset_id is not null"),
    ),
)

item_relevance = sa.Table(
    "item_relevance",
    metadata,
    _pk(),
    sa.Column("item_id", BigId, sa.ForeignKey("items.id"), nullable=False, index=True),
    sa.Column("annotation_id", BigId, sa.ForeignKey("annotations.id")),
    sa.Column("relevance_type", _enum("relevance_type", RELEVANCE_TYPES), nullable=False),
    sa.Column("relevant_from", UTCDateTime),
    sa.Column("relevant_to", UTCDateTime),
    sa.Column("date_precision", _enum("date_precision", DATE_PRECISIONS)),
    sa.Column("raw_phrase", sa.Text),
    sa.Column("anchor_ts", UTCDateTime),
    sa.Column("resolved_by", sa.String(16)),  # llm | rule | manual
    sa.Column("needs_review", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Index(None, "relevant_from"),
    sa.Index(None, "relevance_type", "relevant_from"),
)

annotations = sa.Table(
    "annotations",
    metadata,
    _pk(),
    sa.Column("item_id", BigId, sa.ForeignKey("items.id"), nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("prompt_version", sa.Text, nullable=False),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("payload_json", Json, nullable=False),
    sa.Column("input_tokens", sa.Integer),
    sa.Column("output_tokens", sa.Integer),
    sa.Column("cost_estimate", sa.Numeric(12, 6, asdecimal=False)),
    sa.UniqueConstraint("item_id", "prompt_version"),
)

# Every LLM request (whatever the outcome). Also the source of truth for local quota tracking,
# so a restart doesn't forget how many requests were already made today.
llm_calls = sa.Table(
    "llm_calls",
    metadata,
    _pk(),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("prompt_version", sa.Text),
    sa.Column("started_at", UTCDateTime, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),  # ok | quota_day | quota_minute | error
    sa.Column("n_items", sa.Integer),
    sa.Column("input_tokens", sa.Integer),
    sa.Column("output_tokens", sa.Integer),
    sa.Column("cost_estimate", sa.Numeric(12, 6, asdecimal=False)),
    sa.Column("error", sa.Text),
    sa.Index(None, "model", "started_at"),
)

# Items the LLM failed to annotate validly after a retry; skipped for this prompt_version.
annotation_dead_letters = sa.Table(
    "annotation_dead_letters",
    metadata,
    _pk(),
    sa.Column("item_id", BigId, sa.ForeignKey("items.id"), nullable=False),
    sa.Column("prompt_version", sa.Text, nullable=False),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("error", sa.Text, nullable=False),
    sa.Column("payload_json", Json),
    sa.UniqueConstraint("item_id", "prompt_version"),
)

asset_resolution_queue = sa.Table(
    "asset_resolution_queue",
    metadata,
    _pk(),
    sa.Column("item_id", BigId, sa.ForeignKey("items.id"), nullable=False),
    sa.Column("annotation_id", BigId, sa.ForeignKey("annotations.id")),
    sa.Column("asset_class", _enum("asset_class", ASSET_CLASSES)),
    sa.Column("symbol_or_name", sa.Text, nullable=False),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("resolved_asset_id", BigId, sa.ForeignKey("assets.id")),
    sa.Column("resolved_at", UTCDateTime),
    sa.Index(None, "resolved_at"),
)

# --- market data ------------------------------------------------------------

econ_events = sa.Table(
    "econ_events",
    metadata,
    _pk(),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("event_name", sa.Text, nullable=False),
    sa.Column("country", sa.Text),
    sa.Column("currency", sa.Text),
    sa.Column("scheduled_at", UTCDateTime, nullable=False),
    sa.Column("actual", sa.Float),
    sa.Column("forecast", sa.Float),
    sa.Column("previous", sa.Float),
    sa.Column("unit", sa.Text),
    sa.Column("importance", sa.SmallInteger),
    # forecast gets revised: every fetch that changes values is a new snapshot row
    sa.Column("snapshot_at", UTCDateTime, nullable=False),
    sa.Column(
        "surprise",
        sa.Float,
        sa.Computed("actual - forecast", persisted=True),
    ),
    sa.Index(None, "event_name", "country", "scheduled_at", "snapshot_at"),
    sa.Index(None, "scheduled_at"),
)

rate_expectations = sa.Table(
    "rate_expectations",
    metadata,
    _pk(),
    sa.Column("central_bank", sa.Text, nullable=False),
    sa.Column("meeting_date", sa.Date, nullable=False),
    sa.Column("snapshot_at", UTCDateTime, nullable=False),
    sa.Column("outcome", sa.Text, nullable=False),  # e.g. "425-450" target range in bp
    sa.Column("probability", sa.Float, nullable=False),
    sa.UniqueConstraint("central_bank", "meeting_date", "snapshot_at", "outcome"),
)

# Macro time series (FRED). The first published value is kept: later revisions are ignored,
# so the table shows what the market saw on release.
macro_observations = sa.Table(
    "macro_observations",
    metadata,
    sa.Column("series_id", sa.Text, nullable=False),  # e.g. DGS2, CPIAUCSL
    sa.Column("obs_date", sa.Date, nullable=False),
    sa.Column("value", sa.Float, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("fetched_at", UTCDateTime, nullable=False),
    sa.PrimaryKeyConstraint("series_id", "obs_date"),
)

prices = sa.Table(
    "prices",
    metadata,
    sa.Column("asset_id", BigId, sa.ForeignKey("assets.id"), nullable=False),
    sa.Column("interval", sa.String(8), nullable=False),  # 1m | 5m | 1h | 1d
    sa.Column("ts", UTCDateTime, nullable=False),  # bar open time
    sa.Column("open", sa.Float),
    sa.Column("high", sa.Float),
    sa.Column("low", sa.Float),
    sa.Column("close", sa.Float),
    sa.Column("volume", sa.Float),
    sa.Column("source", sa.Text),
    sa.PrimaryKeyConstraint("asset_id", "interval", "ts"),
)

# Telegram chats subscribed via /start. Rows are never deleted: /stop only deactivates.
subscribers = sa.Table(
    "subscribers",
    metadata,
    sa.Column("chat_id", sa.BigInteger, primary_key=True, autoincrement=False),
    sa.Column("chat_type", sa.String(16)),  # private | group | supergroup | channel
    sa.Column("title", sa.Text),  # @username / first name / group title, for display only
    sa.Column("is_active", sa.Boolean, nullable=False),
    sa.Column("subscribed_at", UTCDateTime, nullable=False),
    sa.Column("unsubscribed_at", UTCDateTime),
    sa.Column("unsubscribe_reason", sa.String(16)),  # stop | blocked
)

# Settings edited from the admin page (e.g. delivery rules), one JSON document per key.
settings = sa.Table(
    "settings",
    metadata,
    sa.Column("key", sa.String(64), primary_key=True),
    sa.Column("value", Json, nullable=False),
    sa.Column("updated_at", UTCDateTime, nullable=False),
)

deliveries = sa.Table(
    "deliveries",
    metadata,
    _pk(),
    sa.Column("item_id", BigId, sa.ForeignKey("items.id"), nullable=False),
    sa.Column("channel", sa.Text, nullable=False),
    sa.Column("sent_at", UTCDateTime),
    sa.Column("message_id", sa.Text),
    sa.Column("status", sa.String(16), nullable=False),  # pending | sent | failed | skipped
    sa.Column("created_at", UTCDateTime),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_error", sa.Text),
    sa.UniqueConstraint("item_id", "channel"),
    sa.Index(None, "status", "channel"),
    sa.Index(None, "sent_at"),
)
