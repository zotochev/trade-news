"""CME FedWatch: market-implied probabilities of FOMC rate decisions (no key).

Uses the unofficial `cme-fedwatch` package (https://github.com/tjdwls101010/CME-FedWatch): it
computes the probabilities with CME's published method from CME fed funds futures settlements
and FRED, and matches the FedWatch Tool to the first decimal. It does its own HTTP (curl_cffi),
outside our rate limiter; a run makes a handful of requests. CME's free feed keeps only about
5 business days, so history exists only from our own snapshots.

Settlements change once per business day, so most runs see the same `trade_date` and do
nothing. On a new trade date every meeting's distribution is stored in `rate_expectations`,
and if an outcome of one of the nearest meetings moved by at least `min_shift_pp` percentage
points since the previous snapshot, one news item describes the shift (it goes through the LLM
and delivery like any other news). The previous snapshot lives in the cursor, so the first run
only stores a baseline.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from trade_news.collectors.base import Batch, Context, RawItem, collector

_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)%-(\d+(?:\.\d+)?)%$")
HINT = "CME FedWatch: market-implied probabilities of Fed rate decisions (USD, US rates)"


def get_probabilities() -> dict:
    from cme_fedwatch import get_probabilities as fetch_all  # heavy import (curl_cffi)

    return fetch_all()


def _lower_bp(outcome: str) -> int | None:
    m = _RANGE_RE.match(outcome.replace(" ", ""))
    return round(float(m.group(1)) * 100) if m else None


def outcome_label(outcome: str, current_target: str) -> str:
    """'4.00%-4.25%' vs current '3.75%-4.00%' -> '25bp hike'."""
    new, cur = _lower_bp(outcome), _lower_bp(current_target)
    if new is None or cur is None:
        return outcome
    diff = new - cur
    if diff == 0:
        return "no change"
    return f"{abs(diff)}bp {'hike' if diff > 0 else 'cut'}"


def _meeting_str(d: str) -> str:
    day = date.fromisoformat(d)  # no %-d: it doesn't exist on Windows
    return f"{day:%b} {day.day}, {day.year}"


def shifts(prev: dict, cur: dict, meetings_ahead: int, min_shift_pp: float) -> list[dict]:
    """Meetings (nearest first) whose largest outcome move is >= min_shift_pp."""
    out = []
    for meeting in sorted(cur)[:meetings_ahead]:
        before, after = prev.get(meeting), cur[meeting]
        if not before:
            continue
        moves = {o: after.get(o, 0.0) - before.get(o, 0.0) for o in set(before) | set(after)}
        # with two outcomes the moves are equal and opposite: name the one the market moves to
        outcome = max(moves, key=lambda o: (abs(moves[o]), moves[o], o))
        if abs(moves[outcome]) >= min_shift_pp:
            out.append(
                {
                    "meeting": meeting,
                    "outcome": outcome,
                    "before": before.get(outcome, 0.0),
                    "after": after.get(outcome, 0.0),
                }
            )
    return out


def shift_item(trade_date: str, target: str, moved: list[dict], cur: dict, prev: dict) -> RawItem:
    top = moved[0]
    label = outcome_label(top["outcome"], target)
    verb = "rose" if top["after"] > top["before"] else "fell"
    title = (
        f"FedWatch: odds of {label if label == 'no change' else 'a ' + label} at the "
        f"{_meeting_str(top['meeting'])} FOMC meeting "
        f"{verb} to {top['after']:.1f}% from {top['before']:.1f}%"
    )
    lines = [f"CME FedWatch, settlement {trade_date}; current target range {target}."]
    for m in moved:
        dist = ", ".join(
            f"{outcome_label(o, target)} {prev[m['meeting']].get(o, 0.0):.1f}% -> {p:.1f}%"
            for o, p in sorted(cur[m["meeting"]].items(), key=lambda kv: -kv[1])
        )
        lines.append(f"{_meeting_str(m['meeting'])} meeting: {dist}.")
    return RawItem(
        source_item_id=f"fedwatch-{trade_date}",
        title=title,
        body="\n".join(lines),
        url="https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
        published_at=None,  # the pipeline uses fetched_at
        raw={"hint": HINT, "trade_date": trade_date, "target": target, "shifts": moved},
    )


@collector(
    "cme_fedwatch",
    description="CME FedWatch: вероятности решений ФРС, сообщение при сильном сдвиге",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    data = get_probabilities()
    trade_date = data["trade_date"]
    if cursor and cursor.get("trade_date") == trade_date:
        return Batch(items=[], cursor=cursor)

    target = data.get("current_target") or ""
    cur = {m["date"]: m["probabilities"] for m in data["meetings"]}
    snapshot_at = datetime.combine(date.fromisoformat(trade_date), datetime.min.time(), UTC)
    rows = [
        {
            "central_bank": "Fed",
            "meeting_date": date.fromisoformat(meeting),
            "snapshot_at": snapshot_at,
            "outcome": outcome,
            "probability": probability,
        }
        for meeting, probs in cur.items()
        for outcome, probability in probs.items()
    ]
    items = []
    prev = (cursor or {}).get("meetings") or {}
    moved = shifts(
        prev,
        cur,
        int(ctx.params.get("meetings_ahead", 3)),
        float(ctx.params.get("min_shift_pp", 10)),
    )
    if moved:
        items.append(shift_item(trade_date, target, moved, cur, prev))
    return Batch(
        items=items,
        cursor={"trade_date": trade_date, "meetings": cur},
        rows={"rate_expectations": rows},
    )
