"""Token unlocks: vesting schedules of crypto projects from DefiLlama (no key).

Team and investor tokens are locked at launch and released on a schedule published up front
(usually a one-year cliff, then monthly or quarterly portions), so dates and sizes are known
years ahead. A large release adds sell pressure.

Data: https://defillama-datasets.llama.fi/emissionsProtocolsList (slugs, ~370) and
`/emissions/<slug>` per project (0.1-17 MB). The api.llama.fi endpoints answer 402 (paid);
`emissionsIndex` (all projects in one 22 MB file) is NOT used: parsing it whole takes too much
memory for the server. Each project file is parsed as a stream (ijson), keeping only:
- `documentedData.data[].data[]`: per allocation section, tokens unlocked by date; the latest
  point <= now summed over sections = supply already unlocked;
- `metadata.unlockEvents[].cliffAllocations[]`: one-off releases (date, recipient, amount).
  Linear (daily) emission is not an event. Category "noncirculating" (treasury, reserves) is
  left out: those tokens are not handed to anyone who sells.
Price and symbol: https://coins.llama.fi/prices/current/<coin>,... (coingecko:<id> or
<chain>:<address>).

Every run refreshes `per_run` projects in rotation and keeps their upcoming releases of at
least `min_pct` of the unlocked supply (cursor + `token_unlocks` table). A release that is
`lead_days` away and worth >= `min_usd` at the current price becomes one news item.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import ijson

from trade_news.collectors.base import Batch, Context, RawItem, collector
from trade_news.collectors.form4 import _usd
from trade_news.collectors.rss import USER_AGENT

BASE = "https://defillama-datasets.llama.fi"
PRICES_URL = "https://coins.llama.fi/prices/current/"
SKIP_CATEGORIES = {"noncirculating"}
_SECTION = "documentedData.data.item"
_POINT = "documentedData.data.item.data.item"
_EVENT = "metadata.unlockEvents.item"


def parse(stream, now: datetime) -> dict:
    """Scans one project file. Returns {name, gecko_id, token, unlocked, cliffs: [(ts, tokens,
    recipients)]}; memory stays at one unlock event, whatever the file size."""
    now_ts = now.timestamp()
    out: dict = {"name": None, "gecko_id": None, "token": None, "cliffs": []}
    sections: list[float] = []
    point: dict = {}
    builder = None
    for prefix, event, value in ijson.parse(stream, use_float=True):
        if builder is not None:
            if prefix == _EVENT and event == "end_map":
                ev, builder = builder.value, None
                out["cliffs"].append(_cliff(ev))
            else:
                builder.event(event, value)
            continue
        if prefix == _EVENT and event == "start_map":
            builder = ijson.ObjectBuilder()
            builder.event(event, value)
        elif prefix == _SECTION and event == "start_map":
            sections.append(0.0)
        elif prefix == _POINT and event == "start_map":
            point = {}
        elif prefix in (_POINT + ".timestamp", _POINT + ".unlocked"):
            point[prefix.rsplit(".", 1)[1]] = value
        elif prefix == _POINT and event == "end_map":
            if (point.get("timestamp") or 0) <= now_ts and point.get("unlocked") is not None:
                sections[-1] = point["unlocked"]
        elif prefix in ("name", "gecko_id", "metadata.token") and event in ("string", "null"):
            out[prefix.removeprefix("metadata.")] = value
    out["unlocked"] = sum(sections)
    out["cliffs"] = _by_day(out["cliffs"])
    return out


def _by_day(cliffs: list[tuple[int, float, list[str]]]) -> list[tuple[int, float, list[str]]]:
    """Releases to several recipients on one day are often separate events: one per day."""
    days: dict = {}
    for ts, tokens, recipients in sorted(cliffs):
        if tokens <= 0:
            continue
        d = days.setdefault(datetime.fromtimestamp(ts, UTC).date(), [ts, 0.0, []])
        d[1] += tokens
        d[2] += [r for r in recipients if r not in d[2]]
    return [tuple(v) for v in days.values()]


def _cliff(ev: dict) -> tuple[int, float, list[str]]:
    allocs = [
        a for a in ev.get("cliffAllocations") or [] if a.get("category") not in SKIP_CATEGORIES
    ]
    recipients = list(dict.fromkeys(a.get("recipient") or "?" for a in allocs))
    return int(ev["timestamp"]), sum(a.get("amount") or 0 for a in allocs), recipients


def coin_id(p: dict) -> str | None:
    return f"coingecko:{p['gecko_id']}" if p.get("gecko_id") else p.get("token")


def prices(ctx: Context, coins: list[str]) -> dict[str, dict]:
    """{coin: {symbol, price}}; coins without a price are missing."""
    out: dict[str, dict] = {}
    for i in range(0, len(coins), 50):
        resp = ctx.get(PRICES_URL + ",".join(coins[i : i + 50]), headers={"User-Agent": USER_AGENT})
        out |= resp.json().get("coins") or {}
    return out


def _item(key: str, e: dict, price: dict, usd: float) -> RawItem:
    at = datetime.fromisoformat(e["at"])
    sym = price.get("symbol") or e["name"]
    who = ", ".join(e["recipients"])
    title = (
        f"Token unlock · {sym} · {e['tokens']:,.0f} tokens ({_usd(usd)}, {e['pct']:.1f}% of "
        f"unlocked supply) on {at:%Y-%m-%d}"
    )
    body = (
        f"Scheduled token unlock of {e['name']} ({sym}) on {at:%Y-%m-%d %H:%M} UTC: a one-off "
        f"release (cliff) of {e['tokens']:,.0f} tokens to {who}. That is {e['pct']:.1f}% of the "
        f"supply unlocked so far, {_usd(usd)} at the current price ${price['price']:,.4g}. "
        "Vesting releases to the team and investors add supply that is often sold."
    )
    return RawItem(
        source_item_id=key,
        title=title,
        body=body,
        url=f"https://defillama.com/unlocks/{key.split('|')[0]}",
        published_at=None,
        raw={
            **e,
            "symbol": sym,
            "price": price["price"],
            "amount_usd": round(usd),
            "hint": f"crypto token unlock (vesting) of {sym}, scheduled for {at:%Y-%m-%d}",
        },
    )


@collector(
    "token_unlocks",
    description="Разблокировки токенов (DefiLlama): крупные выдачи команде и инвесторам",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    now = ctx.now()
    cur = dict(cursor or {})
    per_run = int(ctx.params.get("per_run", 3))
    min_pct = float(ctx.params.get("min_pct", 1.0))
    min_usd = float(ctx.params.get("min_usd", 5_000_000))
    lead = timedelta(days=float(ctx.params.get("lead_days", 3)))
    horizon = timedelta(days=float(ctx.params.get("horizon_days", 365)))
    headers = {"User-Agent": USER_AGENT}

    if not cur.get("protocols") or datetime.fromisoformat(cur["list_at"]) < now - timedelta(days=1):
        slugs = ctx.get(f"{BASE}/emissionsProtocolsList", headers=headers).json()
        if not isinstance(slugs, list):
            raise ValueError(f"unexpected DefiLlama list: {str(slugs)[:300]}")
        cur["protocols"], cur["list_at"] = sorted(slugs), now.isoformat()
    slugs, pos = cur["protocols"], int(cur.get("pos", 0))
    events: dict = {
        k: v
        for k, v in (cur.get("events") or {}).items()
        if datetime.fromisoformat(v["at"]) > now - timedelta(days=1)
    }

    rows = []
    for slug in [slugs[(pos + i) % len(slugs)] for i in range(min(per_run, len(slugs)))]:
        try:
            resp = ctx.get(f"{BASE}/emissions/{slug}", headers=headers)
            p = parse(io.BytesIO(resp.content), now)
            del resp
        except Exception as exc:  # one broken project must not stop the rotation
            ctx.log.warning("unlocks_fetch_failed", slug=slug, error=repr(exc))
            continue
        fresh = {}
        coin = coin_id(p)
        if coin and p["unlocked"] > 0:
            for ts, tokens, recipients in p["cliffs"]:
                at = datetime.fromtimestamp(ts, UTC)
                pct = tokens / p["unlocked"] * 100
                if now < at <= now + horizon and pct >= min_pct:
                    key = f"{slug}|{ts}"
                    fresh[key] = {
                        "name": p["name"] or slug,
                        "coin": coin,
                        "at": at.isoformat(),
                        "tokens": tokens,
                        "pct": round(pct, 2),
                        "recipients": recipients,
                        "notified": events.get(key, {}).get("notified", False),
                    }
                    rows.append({
                        "protocol": slug,
                        "unlock_at": at,
                        "name": p["name"] or slug,
                        "coin": coin,
                        "tokens": tokens,
                        "pct_unlocked": round(pct, 2),
                        "recipients": ", ".join(recipients),
                        "fetched_at": now,
                    })  # fmt: skip
        # the schedule may have been revised: the fresh one replaces what we had
        events = {k: v for k, v in events.items() if not k.startswith(f"{slug}|")} | fresh
    cur["pos"] = (pos + per_run) % max(len(slugs), 1)

    due = {k: e for k, e in events.items()
           if not e["notified"] and datetime.fromisoformat(e["at"]) <= now + lead}  # fmt: skip
    items = []
    if due:
        quotes = prices(ctx, sorted({e["coin"] for e in due.values()}))
        for key, e in due.items():
            q = quotes.get(e["coin"])
            if not q or not q.get("price"):
                continue
            usd = e["tokens"] * q["price"]
            if usd >= min_usd:
                items.append(_item(key, e, q, usd))
                e["notified"] = True
    cur["events"] = events
    return Batch(items=items, cursor=cur, rows={"token_unlocks": rows})
