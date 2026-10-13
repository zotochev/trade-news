"""Crypto leverage snapshots every 5 minutes: open interest, funding, long/short ratio (no keys).

Exchanges serve only current values (Binance keeps 30 days), so the history is ours to keep
(table crypto_derivs, never cleaned). Symbols: `always` (BTC, ETH) + the `top` largest by open
interest on Bybit, re-ranked daily; `exclude` drops non-crypto contracts (gold, stocks).
Per run (~40 requests, rate limit `crypto`):
- Bybit  /v5/market/tickers?category=linear (all symbols in one call: OI value, funding,
  fundingIntervalHour, mark) + /v5/market/account-ratio per symbol (buy/sell accounts);
- Binance /fapi/v1/premiumIndex (all: funding, mark) + /fapi/v1/fundingInfo (non-8h intervals)
  + /fapi/v1/openInterest and /futures/data/globalLongShortAccountRatio per symbol;
- OKX /api/v5/public/open-interest?instType=SWAP (all: oiUsd; USDT and coin-margined swaps are
  summed per coin) + /public/funding-rate and /rubik/.../long-short-account-ratio per coin.
One exchange failing does not drop the others. Funding is stored as the exchange quotes it
(per interval) with the interval; the API normalises to 8 hours.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trade_news.collectors.base import Batch, Context, collector

BYBIT = "https://api.bybit.com/v5/market"
BINANCE = "https://fapi.binance.com"
OKX = "https://www.okx.com/api/v5"
STEP = timedelta(minutes=5)


def _f(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def rank(tickers: list[dict], always: list[str], top: int, exclude: set[str]) -> list[str]:
    """Bases: `always` first, then the largest by open interest value (USDT perpetuals)."""
    rows = [t for t in tickers if t["symbol"].endswith("USDT")]
    rows.sort(key=lambda t: -(_f(t.get("openInterestValue")) or 0))
    out = list(always)
    for t in rows:
        base = t["symbol"].removesuffix("USDT")
        if base not in out and base not in exclude and len(out) < len(always) + top:
            out.append(base)
    return out


def _bybit(ctx: Context, tickers: list[dict], bases: list[str]) -> dict[str, dict]:
    by = {t["symbol"]: t for t in tickers}
    out = {}
    for base in bases:
        t = by.get(f"{base}USDT")
        if not t:
            continue
        ratio = None
        resp = ctx.get(f"{BYBIT}/account-ratio", params={"category": "linear",
                       "symbol": f"{base}USDT", "period": "5min", "limit": 1}).json()  # fmt: skip
        if lst := (resp.get("result") or {}).get("list"):
            buy, sell = _f(lst[0].get("buyRatio")), _f(lst[0].get("sellRatio"))
            ratio = buy / sell if buy and sell else None
        out[base] = {
            "oi_usd": _f(t.get("openInterestValue")),
            "oi_coins": _f(t.get("openInterest")),
            "funding_rate": _f(t.get("fundingRate")),
            "funding_interval_h": _f(t.get("fundingIntervalHour")),
            "long_short_ratio": ratio,
            "mark_price": _f(t.get("markPrice")),
        }
    return out


def _binance(ctx: Context, bases: list[str]) -> dict[str, dict]:
    premium = {p["symbol"]: p for p in ctx.get(f"{BINANCE}/fapi/v1/premiumIndex").json()}
    intervals = {
        i["symbol"]: i.get("fundingIntervalHours")
        for i in ctx.get(f"{BINANCE}/fapi/v1/fundingInfo").json()
    }
    out = {}
    for base in bases:
        sym = f"{base}USDT"
        if sym not in premium:
            continue
        mark = _f(premium[sym].get("markPrice"))
        oi = _f(ctx.get(f"{BINANCE}/fapi/v1/openInterest", params={"symbol": sym})
                .json().get("openInterest"))  # fmt: skip
        ls = ctx.get(f"{BINANCE}/futures/data/globalLongShortAccountRatio",
                     params={"symbol": sym, "period": "5m", "limit": 1}).json()  # fmt: skip
        out[base] = {
            "oi_usd": oi * mark if oi is not None and mark else None,
            "oi_coins": oi,
            "funding_rate": _f(premium[sym].get("lastFundingRate")),
            "funding_interval_h": float(intervals.get(sym) or 8),
            "long_short_ratio": _f(ls[0].get("longShortRatio")) if ls else None,
            "mark_price": mark,
        }
    return out


def _okx(ctx: Context, bases: list[str]) -> dict[str, dict]:
    oi: dict[str, list[float]] = {}
    for row in ctx.get(f"{OKX}/public/open-interest", params={"instType": "SWAP"}).json()["data"]:
        base, quote, _ = row["instId"].split("-", 2)
        if quote in ("USDT", "USD") and base in bases:
            cur = oi.setdefault(base, [0.0, 0.0])
            cur[0] += _f(row.get("oiUsd")) or 0
            cur[1] += _f(row.get("oiCcy")) or 0
    out = {}
    for base in bases:
        if base not in oi:
            continue
        fr = ctx.get(f"{OKX}/public/funding-rate", params={"instId": f"{base}-USDT-SWAP"})
        fr = (fr.json().get("data") or [{}])[0]
        interval = None
        if fr.get("fundingTime") and fr.get("nextFundingTime"):
            interval = (int(fr["nextFundingTime"]) - int(fr["fundingTime"])) / 3_600_000
        ls = ctx.get(f"{OKX}/rubik/stat/contracts/long-short-account-ratio",
                     params={"ccy": base, "period": "5m"}).json().get("data") or []  # fmt: skip
        out[base] = {
            "oi_usd": oi[base][0],
            "oi_coins": oi[base][1],
            "funding_rate": _f(fr.get("fundingRate")),
            "funding_interval_h": interval,
            "long_short_ratio": _f(ls[0][1]) if ls else None,
            "mark_price": oi[base][0] / oi[base][1] if oi[base][1] else None,
        }
    return out


def snapshot_ts(now: datetime) -> datetime:
    epoch = datetime(2020, 1, 1, tzinfo=UTC)
    return epoch + (now - epoch) // STEP * STEP


@collector(
    "crypto_leverage",
    description="Плечо в крипте: открытый интерес, фандинг, лонги/шорты (Binance, Bybit, OKX)",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    now = ctx.now()
    cur = dict(cursor or {})
    p = ctx.params
    tickers = ctx.get(f"{BYBIT}/tickers", params={"category": "linear"}).json()["result"]["list"]
    if not cur.get("symbols") or datetime.fromisoformat(cur["ranked_at"]) < now - timedelta(days=1):
        cur["symbols"] = rank(
            tickers,
            p.get("always", ["BTC", "ETH"]),
            int(p.get("top", 10)),
            set(p.get("exclude", [])),
        )
        cur["ranked_at"] = now.isoformat()
        ctx.log.info("crypto_symbols_ranked", symbols=cur["symbols"])
    bases = cur["symbols"]
    ts = snapshot_ts(now)
    rows = []
    for exchange, get in (
        ("bybit", lambda: _bybit(ctx, tickers, bases)),
        ("binance", lambda: _binance(ctx, bases)),
        ("okx", lambda: _okx(ctx, bases)),
    ):
        try:
            values = get()
        except Exception as exc:  # one exchange down must not lose the others' snapshot
            ctx.log.warning("crypto_exchange_failed", exchange=exchange, error=repr(exc))
            continue
        rows += [{"exchange": exchange, "symbol": b, "ts": ts, **v} for b, v in values.items()]
    return Batch(items=[], cursor=cur, rows={"crypto_derivs": rows})
