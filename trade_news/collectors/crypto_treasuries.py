"""Crypto held by public companies, one snapshot per day (CoinGecko, free, no key).

https://api.coingecko.com/api/v3/companies/public_treasury/<bitcoin|ethereum> -> companies:
name, symbol ("MSTR.US"), country, total_holdings (coins), total_entry_value_usd,
total_current_value_usd, percentage_of_total_supply. CoinGecko keeps no history, so a daily
snapshot is ours (table crypto_treasuries, the first snapshot of a UTC day is kept).
"""

from __future__ import annotations

from trade_news.collectors.base import Batch, Context, collector
from trade_news.collectors.rss import USER_AGENT

URL = "https://api.coingecko.com/api/v3/companies/public_treasury/{coin}"
COINS = {"BTC": "bitcoin", "ETH": "ethereum"}


def parse(data: dict, coin: str, day) -> list[dict]:
    out = []
    for c in data.get("companies") or []:
        if not c.get("name") or c.get("total_holdings") is None:
            continue
        out.append({
            "coin": coin, "snapshot_date": day, "name": c["name"], "symbol": c.get("symbol"),
            "country": c.get("country"), "holdings": float(c["total_holdings"]),
            "entry_value_usd": c.get("total_entry_value_usd"),
            "value_usd": c.get("total_current_value_usd"),
            "pct_supply": c.get("percentage_of_total_supply"),
        })  # fmt: skip
    if not out:
        raise ValueError(f"CoinGecko returned no {coin} treasury companies")
    return out


@collector(
    "crypto_treasuries",
    description="Запасы BTC и ETH у публичных компаний, снимок раз в сутки (CoinGecko)",
    title_dedup=False,
)
def fetch(ctx: Context, cursor: dict | None) -> Batch:
    day = ctx.now().date()
    rows = []
    for coin in ctx.params.get("coins", ["BTC", "ETH"]):
        data = ctx.get(URL.format(coin=COINS[coin]), headers={"User-Agent": USER_AGENT}).json()
        rows += parse(data, coin, day)
    return Batch(items=[], cursor=None, rows={"crypto_treasuries": rows})
