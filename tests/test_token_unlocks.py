import io
import json
from datetime import timedelta
from types import SimpleNamespace

from tests.conftest import NOW, fake_ctx, news_spec
from trade_news.collectors import token_unlocks
from trade_news.pipeline import ingest

DAY = 86400
T0 = int(NOW.timestamp())


def _project(cliffs, unlocked=(600.0, 400.0), gecko="arbitrum"):
    """A tiny emissions file in DefiLlama's layout: sections with a point before and after
    now (the later one must not count), then the unlock events."""
    return {
        "documentedData": {
            "data": [
                {"label": f"s{i}", "data": [
                    {"timestamp": T0 - 30 * DAY, "unlocked": u / 2, "burned": 0},
                    {"timestamp": T0 - DAY, "unlocked": u, "burned": 0},
                    {"timestamp": T0 + 30 * DAY, "unlocked": u * 5, "burned": 0},
                ]}
                for i, u in enumerate(unlocked)
            ],
            "tokenAllocation": {"current": {"insiders": 50}},
        },
        "metadata": {
            "token": "arbitrum:0xabc",
            "unlockEvents": [
                {"timestamp": ts, "cliffAllocations": allocs, "linearAllocations": [],
                 "summary": {}}
                for ts, allocs in cliffs
            ],
        },
        "name": "Arbitrum",
        "gecko_id": gecko,
    }  # fmt: skip


def _alloc(recipient, amount, category="insiders"):
    return {"recipient": recipient, "category": category, "unlockType": "cliff", "amount": amount}


def test_parse_sums_sections_and_merges_same_day_events():
    p = token_unlocks.parse(
        io.BytesIO(json.dumps(_project([
            (T0 + 2 * DAY, [_alloc("Team", 20), _alloc("Treasury", 999, "noncirculating")]),
            (T0 + 2 * DAY + 60, [_alloc("Investors", 10, "privateSale")]),
            (T0 + 40 * DAY, [_alloc("Treasury", 50, "noncirculating")]),
        ])).encode()),
        NOW,
    )  # fmt: skip
    assert (p["name"], p["gecko_id"], p["token"]) == ("Arbitrum", "arbitrum", "arbitrum:0xabc")
    assert p["unlocked"] == 1000  # the latest point <= now in each section
    # treasury left out; the empty treasury-only event dropped
    assert p["cliffs"] == [(T0 + 2 * DAY, 30, ["Team", "Investors"])]
    assert token_unlocks.coin_id(p) == "coingecko:arbitrum"


def _get(project, price=1_000_000.0):
    calls = []

    def get(url, **kw):
        calls.append(url)
        if url.endswith("emissionsProtocolsList"):
            return SimpleNamespace(json=lambda: ["arbitrum"])
        if "/emissions/" in url:
            return SimpleNamespace(content=json.dumps(project).encode())
        coins = {"coingecko:arbitrum": {"symbol": "ARB", "price": price}}
        return SimpleNamespace(json=lambda: {"coins": coins})

    return get, calls


PARAMS = {"per_run": 1, "min_pct": 1.0, "min_usd": 5_000_000, "lead_days": 3}


def test_fetch_keeps_big_releases_and_announces_them_ahead():
    project = _project([
        (T0 + 2 * DAY, [_alloc("Team", 30)]),  # 3% in 2 days, $30M -> news
        (T0 + 20 * DAY, [_alloc("Team", 30)]),  # too far ahead for news, but kept
        (T0 + 25 * DAY, [_alloc("Team", 5)]),  # 0.5%: below min_pct
    ])  # fmt: skip
    get, calls = _get(project)
    batch = token_unlocks.fetch(fake_ctx(get=get, params=PARAMS), None)
    assert [r["unlock_at"] for r in batch.rows["token_unlocks"]] == [
        NOW + timedelta(days=2),
        NOW + timedelta(days=20),
    ]
    (it,) = batch.items
    assert it.source_item_id == f"arbitrum|{T0 + 2 * DAY}"
    assert it.title.startswith("Token unlock · ARB · 30 tokens ($30.0M, 3.0% of unlocked supply)")
    assert it.raw["amount_usd"] == 30_000_000 and "vesting" in it.raw["hint"]

    # next run: announced once only; the list is not fetched again within a day
    calls.clear()
    again = token_unlocks.fetch(fake_ctx(get=get, params=PARAMS), batch.cursor)
    assert again.items == [] and not any(u.endswith("ProtocolsList") for u in calls)
    assert len(again.cursor["events"]) == 2


def test_small_usd_value_is_not_announced():
    get, _ = _get(_project([(T0 + DAY, [_alloc("Team", 30)])]), price=0.01)
    assert token_unlocks.fetch(fake_ctx(get=get, params=PARAMS), None).items == []


def test_rows_are_stored_once(engine, cfg):
    get, _ = _get(_project([(T0 + 20 * DAY, [_alloc("Team", 30)])]))
    spec = news_spec("token_unlocks", title_dedup=False)
    with engine.begin() as conn:
        batch = token_unlocks.fetch(fake_ctx(get=get, params=PARAMS), None)
        assert ingest(conn, spec, batch, cfg, NOW).rows == 1
        batch = token_unlocks.fetch(fake_ctx(get=get, params=PARAMS), batch.cursor)
        assert ingest(conn, spec, batch, cfg, NOW).rows == 0
