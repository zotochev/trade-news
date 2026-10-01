"""Whale in the capital ("Кит в капитале"): a big buyer taking or fighting for a stake.

Kept apart from the main annotation so the main prompt (and every other item's annotation)
stays as it is:
- insider: decided in code from the parsed Form 4 (open-market purchase, code P, >= $500K),
  with cluster_count = distinct purchase days of the same person at the same issuer in 30 days;
- stake / bid / bid_revision / activist: items that look like one (event_type m_and_a or
  keywords) are marked pending at annotation time and sent to a short second prompt.

The result lives in the item's annotation, payload_json["whale"]; items without the key are
not whales. Signal strength is computed here, never by the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import sqlalchemy as sa
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trade_news import views
from trade_news.annotation.assets import equity_by_cik, norm
from trade_news.annotation.contract import LLMUnavailable, QuotaExhausted
from trade_news.db.schema import annotations, item_assets, items, raw_items

log = structlog.get_logger()

WHALE_VERSION = "whale-v1"
INSIDER_MIN_USD = 500_000
CLUSTER_DAYS = 30
NEAR_PP = 5  # "near the takeover threshold": within this many percentage points
PENDING_MAX_WAIT = timedelta(minutes=10)  # delivery holds a pending item at most this long

KINDS = ("stake", "bid", "bid_revision", "activist", "insider")
# mandatory-bid thresholds by listing country; None: no such rule (US)
# EU plus the EEA members (NO IS LI), which apply the same takeover directive
_EU = [
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE",
    "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "NO", "IS", "LI",
]  # fmt: skip
TAKEOVER_THRESHOLDS: dict[str, float | None] = {"US": None, "GB": 30, "AU": 20} | dict.fromkeys(
    _EU, 30
)

# "%" or "holding" would pull in most market news: the words below keep every known whale
CANDIDATE_RE = re.compile(
    r"\b(stakes?|acquir\w*|takeover|take-over|bids?|offers?|tender|buyout|activist|proxy|"
    r"board seats?|13d|merger|approach\w*|proposals?)\b",
    re.I,
)
_FUND_RE = re.compile(r"\bfund\b", re.I)


# --- LLM contract -----------------------------------------------------------------------


class WhaleAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    whale: bool
    target_listed: bool = Field(
        False, description="The target's own shares trade on a stock exchange"
    )
    new_step: bool = Field(
        False,
        description="The text reports a new step by a party: stake bought, bid made, "
        "rejected, raised, campaign launched (not speculation, analysis or a formality)",
    )
    whale_kind: Literal["stake", "bid", "bid_revision", "activist"] | None = None
    buyer: str | None = Field(None, description="Who buys / bids / campaigns")
    buyer_type: Literal["strategic", "private_equity", "activist", "fund", "other"] | None = None
    target_ticker: str | None = Field(None, description="Ticker of the target company")
    exchange: str | None = Field(None, description="Target's main listing, e.g. NASDAQ, ASX")
    listing_country: str | None = Field(
        None, description="ISO 3166-1 alpha-2 country of the target's main listing, e.g. US"
    )
    stake_before_pct: float | None = Field(None, ge=0, le=100)
    stake_after_pct: float | None = Field(None, ge=0, le=100)
    price_per_share: float | None = Field(None, gt=0)
    currency: str | None = Field(None, description="ISO currency of price_per_share")
    conditions: str | None = Field(None, description="Key conditions, short, in Russian")
    rumor: bool = Field(False, description="Unconfirmed: sources say, considering, may")
    denied: bool = Field(False, description="The target or buyer denies it")


class WhaleBatch(BaseModel):
    items: list[WhaleAnswer]


INSTRUCTIONS = """\
You check financial news for a "whale in the capital": a big buyer taking, raising or fighting
for control of a listed company. For EACH input item return one output item with the same id.

The TARGET must be a company whose shares trade on a stock exchange. whale=true only for:
- stake: a strategic buyer, private-equity firm, fund or activist buys or builds a 5–49.9%
  stake in the listed target itself (including raising an existing stake inside that range);
- bid: an offer or proposal to take over / buy out the listed target;
- bid_revision: a bid for the target is rejected, raised or revised, OR a competing /
  alternative / rival proposal arrives for a target that already has a deal or a bid;
- activist: a campaign to sell the target or to change its board.
whale=false for everything else:
- the target is private, a startup, a subsidiary, a unit or a single asset (a listed company
  buying a private business or taking a stake in a unit is NOT a whale);
- opinion, analysis, recaps and "why the stock moved" articles, speculation by commentators
  or analysts about a possible merger;
- buybacks, passive index holdings, routine filings, stakes below 5% or 50% and above;
- formal steps of an already agreed deal: regulatory clearance, shareholder vote, closing,
  final tender results. When unsure: whale=false.
Examples of whale=false: "Top analyst says 80% chance Tesla and SpaceX merge" (speculation);
"More SpaceX merger talks surface" (no party made a move); "Valley National to acquire
Bluevine" (Bluevine is private); "Geely takes 30% of NIO's battery unit" (a unit, not the
listed company); "Skyworks receives all clearances for the Qorvo deal" (formality).
target_listed and new_step must both be true for whale=true.

Fields (null when the text doesn't say; never guess numbers):
- stake_before_pct / stake_after_pct: the buyer's stake in the target, percent;
- price_per_share and currency: the price paid or offered per share;
- listing_country: ISO code of the target's main listing (Agfa → BE, Northern Star → AU);
- conditions: key conditions in Russian, one short line (financing, approvals, deadline);
- rumor=true for reports that are not confirmed by the parties ("sources say", "considering",
  "weighing", "may"); denied=true when a party denies it.
SEC Schedule 13D filings (title "Schedule 13D …"): the body carries the filed facts. A filing
of a new holding or a raised one is a stake; a purpose aimed at the board, a sale or
"strategic alternatives" is activist; a filing about an offer for the issuer is a bid.
Base everything only on the given text."""


def build_prompt(rows: list[dict]) -> str:
    parts = [INSTRUCTIONS, "", "Items:"]
    for r in rows:
        body = " ".join((r.get("body") or "").split())[:1500]
        parts += [
            "",
            f"id: {r['id']}",
            f"published_at: {r['published_at']:%Y-%m-%d}",
            f"title: {r.get('title') or ''}",
            f"body: {body or '(empty)'}",
            f"summary: {r.get('summary') or ''}",
        ]
    return "\n".join(parts)


def response_json_schema() -> dict[str, Any]:
    return WhaleBatch.model_json_schema()


# --- deterministic parts ----------------------------------------------------------------


def takeover_threshold(country: str | None) -> float | None:
    return TAKEOVER_THRESHOLDS.get((country or "").upper())


def strength(w: dict) -> str:
    """The rules in the order of the spec: strong first, then weak, the rest normal. So a
    rejected bid with only a total deal value (no price per share) is still strong."""
    threshold, after = w.get("takeover_threshold_pct"), w.get("stake_after_pct")
    near = threshold is not None and after is not None and abs(threshold - after) <= NEAR_PP
    if near or w.get("whale_kind") == "bid_revision" or (w.get("cluster_count") or 0) >= 2:
        return "strong"
    if w.get("rumor") or w.get("denied"):
        return "weak"
    if w.get("price_per_share") is None and w.get("stake_after_pct") is None:
        return "weak"
    return "normal"


def is_candidate(source: str, title: str, body: str, event_type: str | None) -> bool:
    if source == "sec_edgar":
        return False  # Form 4 is decided in code; other filings carry no text worth it
    return event_type == "m_and_a" or bool(CANDIDATE_RE.search(f"{title} {body}"))


@dataclass
class _Buy:
    total: float
    shares: float
    dates: set[date]


def _buys(form4: dict) -> _Buy:
    trades = [t for t in form4.get("trades") or [] if t.get("code") == "P"]
    total = sum((t.get("shares") or 0) * (t.get("price") or 0) for t in trades)
    # the XML date may carry a zone offset: "2026-09-28-05:00"
    dates = {date.fromisoformat(t["date"][:10]) for t in trades if t.get("date")}
    return _Buy(total, sum(t.get("shares") or 0 for t in trades), dates)


def insider(conn: sa.Connection, raw: dict, at: datetime) -> dict | None:
    """Whale dict for an open-market Form 4 purchase >= $500K, else None."""
    f4 = raw.get("form4") or {}
    if not f4.get("ticker") or _FUND_RE.search(f4.get("issuer_name") or ""):
        return None  # non-traded or fund shares: subscriptions, not market purchases
    buy = _buys(f4)
    if buy.total < INSIDER_MIN_USD:
        return None
    owners = set(f4.get("owners") or [])
    days = set(buy.dates)
    since = at - timedelta(days=CLUSTER_DAYS + 5)
    others = conn.execute(
        sa.select(raw_items.c.raw_json).where(
            raw_items.c.source == "sec_edgar",
            raw_items.c.fetched_at >= since,
            raw_items.c.raw_json[("form4", "issuer_cik")].as_string() == f4.get("issuer_cik"),
        )
    ).scalars()
    start = min(days or {at.date()}) - timedelta(days=CLUSTER_DAYS)
    for other in others:
        o = other.get("form4") or {}
        if owners & set(o.get("owners") or []):
            days |= {d for d in _buys(o).dates if start <= d <= at.date()}
    w = {
        "whale": True,
        "whale_kind": "insider",
        "buyer": ", ".join(sorted(owners)) or None,
        "buyer_type": "insider",
        "buyer_roles": ", ".join(f4.get("roles") or []) or None,
        "target_ticker": f4.get("ticker"),
        "listing_country": None,
        "stake_before_pct": None,
        "stake_after_pct": None,
        "price_per_share": round(buy.total / buy.shares, 4) if buy.shares else None,
        "currency": "USD",
        "amount_usd": round(buy.total),
        "conditions": None,
        "rumor": False,
        "denied": False,
        "cluster_count": len(days),
        "takeover_threshold_pct": None,
        "source": "form4",
        "version": WHALE_VERSION,
    }
    w["strength"] = strength(w)
    return w


def from_answer(a: WhaleAnswer, model: str) -> dict:
    if not (a.whale and a.target_listed and a.new_step) or a.whale_kind is None:
        return {"whale": False, "version": WHALE_VERSION, "model": model}
    w = a.model_dump(exclude={"id"})
    w |= {
        "cluster_count": None,
        "takeover_threshold_pct": takeover_threshold(a.listing_country),
        "source": "llm",
        "version": WHALE_VERSION,
        "model": model,
    }
    w["strength"] = strength(w)
    return w


def _main_asset(conn: sa.Connection, item_id: int) -> int | None:
    return conn.execute(
        sa.select(item_assets.c.asset_id)
        .where(
            item_assets.c.item_id == item_id,
            item_assets.c.annotation_id.in_(views.latest_annotation_ids()),
            item_assets.c.asset_id.is_not(None),
        )
        .order_by(item_assets.c.is_primary.desc(), item_assets.c.importance.desc())
        .limit(1)
    ).scalar()


def _buyer_key(buyer: str | None) -> str:
    words = norm(buyer or "").split()
    return words[0] if words else ""


def competing_bid(conn: sa.Connection, item_id: int, buyer: str | None, at: datetime) -> bool:
    """A bid for an asset that already had a bid from someone else in the last 30 days: a
    competing bidder, i.e. bid_revision. Decided in code: the LLM sees one item at a time."""
    asset_id, me = _main_asset(conn, item_id), _buyer_key(buyer)
    if asset_id is None or not me:
        return False
    kind = annotations.c.payload_json[("whale", "whale_kind")].as_string()
    prior = conn.execute(
        sa.select(annotations.c.payload_json[("whale", "buyer")].as_string())
        .join(items, items.c.id == annotations.c.item_id)
        .join(
            item_assets,
            sa.and_(
                item_assets.c.annotation_id == annotations.c.id, item_assets.c.asset_id == asset_id
            ),
        )
        .where(
            views.current_annotation(),
            kind.in_(["bid", "bid_revision"]),
            items.c.id != item_id,
            items.c.published_at >= at - timedelta(days=CLUSTER_DAYS),
            items.c.published_at < at,
        )
    ).scalars()
    return any(_buyer_key(b) not in ("", me) for b in prior)


# --- at annotation time and the second pass ----------------------------------------------


def initial(conn: sa.Connection, item_id: int, source: str, title: str, body: str,
            event_type: str | None, at: datetime) -> dict | None:  # fmt: skip
    """payload_json["whale"] right after the main annotation: an insider decision, a pending
    marker for the second pass, or None (not a candidate)."""
    if source == "sec_edgar":
        raw = conn.execute(
            sa.select(raw_items.c.raw_json)
            .join(items, items.c.raw_item_id == raw_items.c.id)
            .where(items.c.id == item_id)
        ).scalar()
        raw = raw or {}
        if raw.get("schedule13d"):  # a stake filing: the second pass reads its purpose
            pct = _13d_pct(raw["schedule13d"])
            ok = pct is not None and MIN_STAKE_PCT <= pct < MAX_STAKE_PCT
            return {"pending": True, "version": WHALE_VERSION} if ok else None
        return insider(conn, raw, at)
    if is_candidate(source, title, body, event_type):
        return {"pending": True, "version": WHALE_VERSION}
    return None


PENDING = annotations.c.payload_json[("whale", "pending")].as_boolean().is_(True)


def resolve_pending(engine: sa.Engine, client, batch_size: int = 20, limit: int = 100) -> int:
    """Second pass over pending candidates. Returns items resolved. Quota / provider trouble
    leaves them pending (delivery stops waiting after PENDING_MAX_WAIT)."""
    with engine.connect() as conn:
        rows = [
            dict(r._mapping)
            for r in conn.execute(
                sa.select(
                    annotations.c.id.label("annotation_id"),
                    items.c.id,
                    items.c.title,
                    items.c.body,
                    items.c.published_at,
                    annotations.c.payload_json["summary"].as_string().label("summary"),
                    raw_items.c.raw_json,
                )
                .join(items, items.c.id == annotations.c.item_id)
                .join(raw_items, raw_items.c.id == items.c.raw_item_id)
                .where(PENDING)
                .order_by(annotations.c.id.desc())
                .limit(limit)
            )
        ]
    done = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        try:
            model, parsed = client.generate_json(
                build_prompt(batch), response_json_schema(), len(batch), WHALE_VERSION
            )
        except (QuotaExhausted, LLMUnavailable) as exc:
            log.warning("whale_pass_stopped", error=str(exc), pending=len(rows) - i)
            break
        answers: dict[int, WhaleAnswer] = {}
        for raw in (parsed or {}).get("items") or []:
            try:
                a = WhaleAnswer.model_validate(raw)
            except ValidationError:
                continue
            answers[a.id] = a
        with engine.begin() as conn:
            for r in batch:
                a = answers.get(r["id"])
                filing = (r["raw_json"] or {}).get("schedule13d")
                if a and filing:  # a 13D is itself the new step, by an SEC-registered issuer
                    a = a.model_copy(update={"target_listed": True, "new_step": True})
                w = (
                    from_answer(a, model)
                    if a
                    else {"whale": False, "version": WHALE_VERSION, "error": "no valid answer"}
                )
                if filing and w.get("whale"):
                    apply_13d(conn, w, filing, r["published_at"])
                apply_competing(conn, r["id"], w, r["published_at"])
                _store(conn, r["annotation_id"], w)
                done += 1
    if rows:
        log.info("whale_pass_done", candidates=len(rows), resolved=done)
    return done


MIN_STAKE_PCT, MAX_STAKE_PCT = 5, 50


def _13d_pct(filing: dict) -> float | None:
    values = [p["pct"] for p in filing.get("persons") or [] if p.get("pct") is not None]
    return max(values) if values else None


def _13d_filer(filing: dict) -> str | None:
    persons = filing.get("persons") or []
    return max(persons, key=lambda p: p.get("pct") or 0)["name"] if persons else None


def previous_13d_pct(conn: sa.Connection, filing: dict, at: datetime) -> float | None:
    """The holding in the latest earlier 13D of the same filer (any reporting person in common)
    at the same issuer, from our own data; None when we haven't seen one."""
    names = {p["name"] for p in filing.get("persons") or []}
    rows = conn.execute(
        sa.select(raw_items.c.raw_json)
        .where(
            raw_items.c.source == "sec_edgar",
            raw_items.c.published_at < at,
            raw_items.c.raw_json[("schedule13d", "issuer_cik")].as_string()
            == filing.get("issuer_cik"),
        )
        .order_by(raw_items.c.published_at.desc())
    ).scalars()
    for raw in rows:
        other = raw.get("schedule13d") or {}
        if names & {p["name"] for p in other.get("persons") or []}:
            return _13d_pct(other)
    return None


def apply_13d(conn: sa.Connection, w: dict, filing: dict, at: datetime) -> None:
    """The filed numbers win over the LLM's reading. A lower or unchanged holding is no whale
    (an amendment reports the new holding, the previous one comes from our data)."""
    after = _13d_pct(filing)
    before = previous_13d_pct(conn, filing, at) if filing.get("amendment_no") is not None else None
    if (
        w.get("whale_kind") == "stake"
        and before is not None
        and after is not None
        and (after <= before)
    ):
        w.clear()
        w.update({"whale": False, "version": WHALE_VERSION, "reason": "13D: holding not raised"})
        return
    w["stake_after_pct"], w["stake_before_pct"] = after, before
    w["buyer"] = w.get("buyer") or _13d_filer(filing)
    if not w.get("target_ticker") and filing.get("issuer_cik"):
        w["target_ticker"] = equity_by_cik(conn, filing["issuer_cik"])
    w["listing_country"] = w.get("listing_country") or "US"
    w["takeover_threshold_pct"] = takeover_threshold(w["listing_country"])
    w["source"] = "13d"
    w["strength"] = strength(w)


def apply_competing(conn: sa.Connection, item_id: int, w: dict, at: datetime) -> None:
    if w.get("whale_kind") == "bid" and competing_bid(conn, item_id, w.get("buyer"), at):
        w["whale_kind"], w["competing"] = "bid_revision", True
        w["strength"] = strength(w)


def _store(conn: sa.Connection, annotation_id: int, whale: dict) -> None:
    payload = conn.execute(
        sa.select(annotations.c.payload_json).where(annotations.c.id == annotation_id)
    ).scalar()
    conn.execute(
        annotations.update()
        .where(annotations.c.id == annotation_id)
        .values(payload_json={**(payload or {}), "whale": whale})
    )


def whale_of(item: dict) -> dict | None:
    """The whale dict of an item read by views.news_item, when it is one."""
    w = (item.get("payload_json") or {}).get("whale")
    return w if w and w.get("whale") and w.get("whale_kind") in KINDS else None
