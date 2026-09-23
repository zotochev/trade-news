"""LLM annotation contract (spec, section 4). Provider-agnostic.

The domain talks to any LLM through `LLMClient.annotate_batch`. Providers get the prompt from
`build_prompt` and the output JSON Schema from `response_json_schema` (generated from the
pydantic models below, so validation and the schema sent to the model never drift apart).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# Bump when the prompt or schema changes: annotations are unique per (item, prompt_version),
# so a new version re-annotates items and old results stay for comparison.
PROMPT_VERSION = "v1"

AssetClass = Literal["equity", "fx", "crypto", "commodity", "index", "rates", "macro"]
Scope = Literal["market_wide", "group", "specific"]
Direction = Literal["bullish", "bearish", "neutral"]
RelevanceType = Literal["immediate", "scheduled", "window", "unknown"]
DatePrecision = Literal["exact", "day", "month", "quarter", "unknown"]
EventType = Literal[
    "earnings", "guidance", "m_and_a", "regulatory", "macro", "rate_decision", "cb_speech",
    "insider", "other",
]  # fmt: skip


# --- input / output of a provider ---------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemForAnnotation:
    id: int
    source: str
    published_at: datetime
    title: str
    body: str
    hints: str | None = None  # e.g. "issuer ticker: AAPL" from the source's own metadata


@dataclass(frozen=True, slots=True)
class Annotation:
    """One item's raw result. `payload` is not validated yet: the domain does that."""

    item_id: int
    payload: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    cost_estimate: float


class QuotaExhausted(Exception):
    """No model has quota left for now. Items stay pending and are retried in the next window."""


class LLMUnavailable(Exception):
    """Transient provider failure after retries."""


class LLMClient(Protocol):
    provider: str

    def annotate_batch(self, items: list[ItemForAnnotation]) -> list[Annotation]:
        """Returns annotations for the items the model answered (possibly a subset).
        Raises QuotaExhausted / LLMUnavailable."""
        ...


# --- output schema --------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AssetLink(_Strict):
    asset_class: AssetClass
    scope: Scope
    symbol_or_name: str | None = Field(
        description="Ticker or name for scope=specific (AAPL, EUR/USD, BTC, Gold); null otherwise"
    )
    group_label: str | None = Field(
        description="Sector/region/group for scope=group (tech, EM currencies); null otherwise"
    )
    direction: Direction | None = Field(description="Expected effect on THIS asset")
    importance: int = Field(ge=1, le=5, description="1=noise … 5=market-moving, for THIS asset")
    is_primary: bool = Field(description="The main subject of the news, not a passing mention")
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _scope_fields(self):
        if self.scope == "specific" and not (self.symbol_or_name or "").strip():
            raise ValueError("scope=specific requires symbol_or_name")
        if self.scope == "group" and not (self.group_label or "").strip():
            raise ValueError("scope=group requires group_label")
        return self


class Relevance(_Strict):
    type: RelevanceType
    date_iso: str | None = Field(
        description="Start of applicability, YYYY-MM-DD or full ISO datetime, resolved from "
        "published_at. null for immediate/unknown"
    )
    date_to_iso: str | None = Field(description="End of the window for type=window, else null")
    date_precision: DatePrecision
    raw_phrase: str | None = Field(description="The date expression exactly as in the text")


class ItemAnnotation(_Strict):
    id: int
    assets: list[AssetLink]
    relevance: Relevance
    event_type: EventType
    summary: str = Field(description="One line in Russian: the essence of the news")


class BatchResponse(_Strict):
    items: list[ItemAnnotation]


def response_json_schema() -> dict[str, Any]:
    return BatchResponse.model_json_schema()


def validate_item(payload: dict[str, Any]) -> ItemAnnotation | str:
    """Parsed annotation, or a short error message."""
    try:
        return ItemAnnotation.model_validate(payload)
    except ValidationError as exc:
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])


# --- prompt -----------------------------------------------------------------------

INSTRUCTIONS = """\
You annotate financial news for a market-data system. For EACH input item return one output
item with the same "id".

assets: every asset the news is relevant to. One news item may have several links with
different scope:
- asset_class: equity | fx | crypto | commodity | index | rates | macro
- scope: market_wide (the whole class, e.g. "US stocks fall"), group (sector/region/group,
  set group_label), specific (one instrument, set symbol_or_name: prefer the ticker/pair —
  AAPL, EUR/USD, BTC, XAU/USD, US10Y, SPX)
- direction and importance are for THAT asset: a strong dollar is bearish for EUR/USD and
  bullish for DXY. importance: 1 noise, 2 minor, 3 notable, 4 important, 5 market-moving.
- is_primary: true only for the main subject(s).
Example: a Fed decision → (rates, market_wide) + (fx, specific, EUR/USD) +
(equity, market_wide) + (crypto, market_wide).
Routine filings (e.g. an ordinary insider Form 4, trust/ABS 8-Ks) get importance 1-2.

relevance: when the news applies.
- immediate: it already happened (date_iso null).
- scheduled: a future event (meeting, earnings date, lock-up expiry, ex-dividend date).
- window: applies over a period (date_iso start, date_to_iso end).
- unknown: no date can be extracted.
CRITICAL: resolve relative dates ("next Thursday", "tomorrow", "в следующий четверг", "on
Friday") FROM THE ITEM'S published_at, NEVER from today's date. Put the original expression
into raw_phrase. date_precision: exact (time known), day, month, quarter, unknown.

summary: one short line in Russian with the essence (who, what, key number).
Base everything only on the given text; do not invent facts."""


def _weekday(dt: datetime) -> str:
    return dt.strftime("%A")


def build_prompt(items: list[ItemForAnnotation], body_max_chars: int = 1500) -> str:
    parts = [INSTRUCTIONS, "", "Items:"]
    for it in items:
        body = " ".join((it.body or "").split())[:body_max_chars]
        parts += [
            "",
            f"id: {it.id}",
            f"source: {it.source}",
            f"published_at: {it.published_at.isoformat()} ({_weekday(it.published_at)})",
            *([f"hints: {it.hints}"] if it.hints else []),
            f"title: {it.title}",
            f"body: {body or '(empty)'}",
        ]
    return "\n".join(parts)
