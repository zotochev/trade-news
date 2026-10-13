"""Forced liquidations from the exchanges' public websocket streams, summed per minute.

One thread per exchange (reconnects with backoff) feeds an in-memory aggregator; a flusher
writes finished minutes to crypto_liquidations every 15 s (a late message for a minute already
written is added to it). Symbols outside the tracked list (the crypto_leverage collector's
cursor, re-read every 10 min) go to OTHER, so a cascade in small coins is still counted.

- Binance  wss://fstream.binance.com/ws/!forceOrder@arr: all symbols, but Binance sends at most
  the latest liquidation per symbol per second: an UNDERCOUNT in a cascade.
  o.S = SELL -> a long was liquidated; USD = o.z (filled qty) x o.ap (average price).
- Bybit    wss://stream.bybit.com/v5/public/linear, topic allLiquidation.<SYMBOL>: complete, but
  per symbol, so only the tracked ones (no OTHER). S = Buy -> a long was liquidated (Bybit
  docs); USD = v x p. Ping every 20 s.
- OKX      wss://ws.okx.com:8443/ws/v5/public, channel liquidation-orders, instType SWAP: all
  symbols. sz is in contracts: USD = sz x ctVal x bkPx (ctVal from /public/instruments; for
  coin-margined swaps ctVal is already USD). posSide long, or net with side sell -> long.
  "ping" every 25 s.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import sqlalchemy as sa
import structlog
from websockets.sync.client import connect

from trade_news.db.engine import _insert
from trade_news.db.schema import collector_state, crypto_liquidations

log = structlog.get_logger()

OTHER = "OTHER"
Event = tuple[str, str, datetime, str, float]  # exchange, symbol, time, side, usd


class Aggregator:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buckets: dict[tuple[str, str, datetime, str], list[float]] = {}
        self.tracked: set[str] = {"BTC", "ETH"}

    def add(self, exchange: str, base: str, at: datetime, side: str, usd: float) -> None:
        if usd <= 0 or side not in ("long", "short"):
            return
        minute = at.replace(second=0, microsecond=0)
        key = (exchange, base if base in self.tracked else OTHER, minute, side)
        with self.lock:
            b = self.buckets.setdefault(key, [0.0, 0])
            b[0] += usd
            b[1] += 1

    def take(self, before: datetime) -> list[dict]:
        """Removes and returns the minutes that ended before `before`."""
        with self.lock:
            done = [k for k in self.buckets if k[2] < before]
            out = [
                {"exchange": k[0], "symbol": k[1], "minute": k[2], "side": k[3],
                 "usd": self.buckets[k][0], "count": int(self.buckets[k][1])}
                for k in done
            ]  # fmt: skip
            for k in done:
                del self.buckets[k]
        return out


def write(conn: sa.Connection, rows: list[dict]) -> None:
    """Insert; a minute already written gets the late part added."""
    if not rows:
        return
    stmt = _insert(conn, crypto_liquidations)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "exchange", "minute", "side"],
        set_={
            "usd": crypto_liquidations.c.usd + stmt.excluded.usd,
            "count": crypto_liquidations.c.count + stmt.excluded["count"],
        },
    )
    conn.execute(stmt, rows)


# --- parsing (pure, tested) ------------------------------------------------------------


def _ms(ts) -> datetime:
    return datetime.fromtimestamp(int(ts) / 1000, UTC)


def parse_binance(msg: dict) -> list[Event]:
    o = msg.get("o") or {}
    sym = o.get("s") or ""
    if not sym.endswith("USDT"):
        return []
    usd = float(o.get("z") or o.get("q") or 0) * float(o.get("ap") or o.get("p") or 0)
    side = "long" if o.get("S") == "SELL" else "short"
    return [("binance", sym.removesuffix("USDT"), _ms(o.get("T") or msg["E"]), side, usd)]


def parse_bybit(msg: dict) -> list[Event]:
    if not str(msg.get("topic", "")).startswith("allLiquidation."):
        return []
    out = []
    for d in msg.get("data") or []:
        sym = d.get("s") or ""
        side = "long" if d.get("S") == "Buy" else "short"
        usd = float(d.get("v") or 0) * float(d.get("p") or 0)
        out.append(("bybit", sym.removesuffix("USDT"), _ms(d["T"]), side, usd))
    return out


def parse_okx(msg: dict, ct: dict[str, tuple[float, bool]]) -> list[Event]:
    out = []
    for d in msg.get("data") or []:
        inst = d.get("instId") or ""
        base, quote = inst.split("-")[:2] if inst.count("-") >= 2 else ("", "")
        if quote not in ("USDT", "USD") or inst not in ct:
            continue
        ct_val, in_usd = ct[inst]
        for x in d.get("details") or []:
            sz, px = float(x.get("sz") or 0), float(x.get("bkPx") or 0)
            usd = sz * ct_val if in_usd else sz * ct_val * px
            pos = x.get("posSide")
            side = (
                pos
                if pos in ("long", "short")
                else ("long" if x.get("side") == "sell" else "short")
            )
            out.append(("okx", base, _ms(x["ts"]), side, usd))
    return out


# --- streams --------------------------------------------------------------------------


def _okx_contracts() -> dict[str, tuple[float, bool]]:
    data = httpx.get("https://www.okx.com/api/v5/public/instruments",
                     params={"instType": "SWAP"}, timeout=30).json()["data"]  # fmt: skip
    return {i["instId"]: (float(i["ctVal"]), i.get("ctValCcy") == "USD") for i in data}


def _run_stream(name: str, agg: Aggregator, stop: threading.Event) -> None:
    backoff = 5
    while not stop.is_set():
        started = time.monotonic()
        try:
            _stream_once(name, agg, stop)
        except Exception as exc:
            log.warning("liquidations_stream_error", exchange=name, error=repr(exc))
        if time.monotonic() - started > 300:
            backoff = 5  # it was up for a while: reconnect fast
        stop.wait(backoff)
        backoff = min(backoff * 2, 300)


def _stream_once(name: str, agg: Aggregator, stop: threading.Event) -> None:
    if name == "binance":
        url, subscribe, ping, parse = (
            "wss://fstream.binance.com/ws/!forceOrder@arr",
            None,
            None,
            parse_binance,
        )
    elif name == "bybit":
        tracked = sorted(agg.tracked)
        url = "wss://stream.bybit.com/v5/public/linear"
        topics = [f"allLiquidation.{s}USDT" for s in tracked]
        subscribe = [
            {"op": "subscribe", "args": topics[i : i + 10]} for i in range(0, len(topics), 10)
        ]
        ping, parse = (20, json.dumps({"op": "ping"})), parse_bybit
    else:
        ct = _okx_contracts()
        url = "wss://ws.okx.com:8443/ws/v5/public"
        subscribe = [
            {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}
        ]
        ping, parse = (25, "ping"), (lambda m: parse_okx(m, ct))
    tracked_then = set(agg.tracked)
    with connect(url, open_timeout=15, close_timeout=5) as ws:
        for s in subscribe or []:
            ws.send(json.dumps(s))
        log.info("liquidations_stream_connected", exchange=name)
        last_ping = time.monotonic()
        opened = time.monotonic()
        while not stop.is_set():
            if ping and time.monotonic() - last_ping > ping[0]:
                ws.send(ping[1])
                last_ping = time.monotonic()
            if name == "bybit" and agg.tracked != tracked_then:
                return  # resubscribe to the new symbol list
            if name == "okx" and time.monotonic() - opened > 86400:
                return  # refresh contract sizes daily
            try:
                raw = ws.recv(timeout=5)
            except TimeoutError:
                continue
            if raw == "pong":
                continue
            msg = json.loads(raw)
            for ex, base, at, side, usd in parse(msg):
                agg.add(ex, base, at, side, usd)


def _flusher(engine: sa.Engine, agg: Aggregator, stop: threading.Event, now: Callable[[], datetime],
             lock) -> None:  # fmt: skip
    last_symbols = 0.0
    while not stop.wait(15):
        try:
            if time.monotonic() - last_symbols > 600:
                with engine.connect() as conn:
                    cur = conn.execute(
                        sa.select(collector_state.c.cursor).where(
                            collector_state.c.source == "crypto_leverage"
                        )
                    ).scalar()
                if cur and cur.get("symbols"):
                    agg.tracked = set(cur["symbols"])
                last_symbols = time.monotonic()
            rows = agg.take(now().replace(second=0, microsecond=0) - timedelta(minutes=1))
            if rows:
                with lock, engine.begin() as conn:
                    write(conn, rows)
        except Exception:
            log.exception("liquidations_flush_failed")


def start_in_thread(engine: sa.Engine, lock, now: Callable[[], datetime],
                    exchanges=("binance", "bybit", "okx")) -> threading.Event:  # fmt: skip
    """Starts the streams and the flusher (daemon threads). Returns the stop event."""
    stop = threading.Event()
    agg = Aggregator()
    threading.Thread(target=_flusher, args=(engine, agg, stop, now, lock),
                     name="liquidations-flush", daemon=True).start()  # fmt: skip
    for name in exchanges:
        threading.Thread(target=_run_stream, args=(name, agg, stop),
                         name=f"liquidations-{name}", daemon=True).start()  # fmt: skip
    return stop
