"""Order book depth every 5 minutes: resting bids / asks in USD near mid (no keys).

The «стакан» link of the crypto model: how much passive liquidity stands within ±0.1 / 0.5 / 1 %
of mid, spot vs perpetual. Only the sums are kept (table crypto_orderbook, never cleaned).
Per run, per coin (8 requests, rate limit `crypto`):
- Binance spot  /api/v3/depth limit=5000 (weight 250; reaches ~±1 % on BTC, ~±4 % on ETH);
- Binance perp  /fapi/v1/depth limit=1000 (max; ~±0.15 % on BTC);
- Bybit perp    /v5/market/orderbook limit=500 (REST max; ~±0.1 % on BTC);
- OKX perp      /market/books-full sz=5000 (~±0.9 % on BTC; sizes in contracts × ctVal).
A side that the served book doesn't reach to a threshold gives None, not a partial sum. OKX
books-full allows one call per 2 s, so its two coins are requested at both ends of the run.
One venue failing does not drop the others.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from trade_news.collectors.base import Batch, Context, collector
from trade_news.collectors.crypto_leverage import snapshot_ts

SPOT = "https://api.binance.com/api/v3/depth"
PERP = "https://fapi.binance.com/fapi/v1/depth"
OKX = "https://www.okx.com/api/v5"
BANDS = {"01": 0.001, "05": 0.005, "1": 0.01}
# levels requested per side (the venues' maxima); fewer back means the whole side was sent
LIMITS = {"binance_spot": 5000, "binance_perp": 1000, "bybit_perp": 500, "okx_perp": 5000}


def depth(bids: list[tuple[float, float]], asks: list[tuple[float, float]], limit: int) -> dict:
    """Levels (price, size in coins), best first -> mid, spread and USD sums per band."""
    mid = (bids[0][0] + asks[0][0]) / 2
    out = {"mid": mid, "spread": round(asks[0][0] - bids[0][0], 8)}
    for side, levels, sign in (("bid", bids, -1), ("ask", asks, 1)):
        full = len(levels) < limit  # the venue sent the whole side
        for key, band in BANDS.items():
            edge = mid * (1 + sign * band)
            reached = full or (levels[-1][0] <= edge if sign < 0 else levels[-1][0] >= edge)
            inside = (p * q for p, q in levels if (p >= edge if sign < 0 else p <= edge))
            out[f"{side}_{key}"] = round(sum(inside), 2) if reached else None
    return out


def _levels(rows, mult: float = 1.0) -> list[tuple[float, float]]:
    return [(float(r[0]), float(r[1]) * mult) for r in rows]


def _binance(ctx: Context, url: str, base: str, limit: int) -> dict:
    d = ctx.get(url, params={"symbol": f"{base}USDT", "limit": limit}).json()
    return depth(_levels(d["bids"]), _levels(d["asks"]), limit)


def _bybit(ctx: Context, base: str) -> dict:
    params = {"category": "linear", "symbol": f"{base}USDT", "limit": LIMITS["bybit_perp"]}
    r = ctx.get("https://api.bybit.com/v5/market/orderbook", params=params).json()["result"]
    return depth(_levels(r["b"]), _levels(r["a"]), LIMITS["bybit_perp"])


def _okx(ctx: Context, base: str, ct_val: float) -> dict:
    params = {"instId": f"{base}-USDT-SWAP", "sz": LIMITS["okx_perp"]}
    d = ctx.get(f"{OKX}/market/books-full", params=params).json()["data"][0]
    return depth(_levels(d["bids"], ct_val), _levels(d["asks"], ct_val), LIMITS["okx_perp"])


def _okx_ct_val(ctx: Context, bases: list[str]) -> dict[str, float]:
    out = {}
    for base in bases:
        params = {"instType": "SWAP", "instId": f"{base}-USDT-SWAP"}
        d = ctx.get(f"{OKX}/public/instruments", params=params).json()
        out[base] = float(d["data"][0]["ctVal"])  # coins per contract
    return out


@collector(
    "crypto_orderbook",
    description="Стакан в крипте: заявки у цены (±0,1/0,5/1%), спот и перпы Binance, Bybit, OKX",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    now = ctx.now()
    cur = dict(cursor or {})
    bases = list(ctx.params.get("symbols", ["BTC", "ETH"]))
    ts = snapshot_ts(now)
    if not cur.get("okx_ct_val") or datetime.fromisoformat(cur["ct_at"]) < now - timedelta(days=1):
        try:
            cur["okx_ct_val"], cur["ct_at"] = _okx_ct_val(ctx, bases), now.isoformat()
        except Exception as exc:
            ctx.log.warning("orderbook_okx_contracts_failed", error=repr(exc))
    ct = cur.get("okx_ct_val") or {}
    jobs = []
    for i, base in enumerate(bases):
        venues = [
            ("binance_spot", lambda b=base: _binance(ctx, SPOT, b, LIMITS["binance_spot"])),
            ("binance_perp", lambda b=base: _binance(ctx, PERP, b, LIMITS["binance_perp"])),
            ("bybit_perp", lambda b=base: _bybit(ctx, b)),
        ]
        if base in ct:
            okx = ("okx_perp", lambda b=base: _okx(ctx, b, ct[b]))
            venues = [okx, *venues] if i % 2 == 0 else [*venues, okx]
        jobs += [(base, venue, get) for venue, get in venues]
    rows = []
    for base, venue, get in jobs:
        try:
            rows.append({"venue": venue, "symbol": base, "ts": ts, **get()})
        except Exception as exc:  # one venue down must not lose the others' snapshot
            ctx.log.warning("orderbook_venue_failed", venue=venue, symbol=base, error=repr(exc))
    return Batch(items=[], cursor=cur, rows={"crypto_orderbook": rows})
