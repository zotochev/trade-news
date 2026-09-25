from datetime import date

import httpx
import pytest
import sqlalchemy as sa

from tests.conftest import NOW, fake_ctx, news_spec
from trade_news.collectors.fred import fetch
from trade_news.db.schema import macro_observations
from trade_news.pipeline import ingest

SECRETS = {"FRED_API_KEY": "secret-key"}
DGS2 = {"id": "DGS2", "name": "US 2-year Treasury yield", "alert_bp": 10, "hint": "US rates"}
OBS = [("2026-09-21", "4.76"), ("2026-09-22", "4.71"), ("2026-09-23", "4.85")]


def fake_get(observations, seen):
    def get(url, params):
        seen.append(params)
        rows = [
            {"date": d, "value": v} for d, v in observations if d >= params["observation_start"]
        ]
        return httpx.Response(200, json={"observations": rows})

    return get


def ctx(get, series=(DGS2,)):
    return fake_ctx(get=get, params={"series": list(series), "lookback_days": 10}, secrets=SECRETS)


def test_first_run_loads_history_without_alerts():
    seen = []
    batch = fetch(ctx(fake_get([*OBS, ("2026-09-20", ".")], seen)), None)
    assert seen[0]["observation_start"] == "2026-09-13"  # NOW - lookback_days
    assert seen[0]["api_key"] == "secret-key"
    assert batch.items == []
    assert [r["value"] for r in batch.rows["macro_observations"]] == [4.76, 4.71, 4.85]
    assert batch.cursor == {"DGS2": {"date": "2026-09-23", "value": 4.85}}


def test_new_big_move_alerts():
    cursor = {"DGS2": {"date": "2026-09-22", "value": 4.71}}
    seen = []
    batch = fetch(ctx(fake_get(OBS, seen)), cursor)
    assert seen[0]["observation_start"] == "2026-09-22"
    assert [r["obs_date"] for r in batch.rows["macro_observations"]] == [date(2026, 9, 23)]
    (it,) = batch.items
    assert it.title == "FRED: US 2-year Treasury yield rose 14 bp to 4.85% (Sep 23, 2026)"
    assert it.source_item_id == "fred-DGS2-2026-09-23"
    assert it.raw["hint"] == "US rates" and it.raw["change_bp"] == 14


def test_small_move_and_series_without_threshold_do_not_alert():
    cursor = {
        "DGS2": {"date": "2026-09-21", "value": 4.76},
        "UNRATE": {"date": "2026-08-01", "value": 4.0},
    }
    obs = [("2026-09-22", "4.71"), ("2026-09-01", "4.9")]
    batch = fetch(ctx(fake_get(obs, []), series=(DGS2, {"id": "UNRATE"})), cursor)
    assert batch.items == []  # DGS2 -5 bp; UNRATE has no alert_bp


def test_only_newest_move_alerts_after_downtime():
    cursor = {"DGS2": {"date": "2026-09-18", "value": 4.40}}
    obs = [("2026-09-21", "4.76"), ("2026-09-22", "4.71"), ("2026-09-23", "4.73")]
    batch = fetch(ctx(fake_get(obs, [])), cursor)
    assert batch.items == []  # +36 bp on Sep 21 is stale; Sep 23 moved only 2 bp


def test_http_error_hides_key():
    def get(url, params):
        req = httpx.Request("GET", url, params=params)
        httpx.Response(400, request=req, text='{"error_message":"Bad Request"}').raise_for_status()

    with pytest.raises(RuntimeError, match="Bad Request") as exc:
        fetch(ctx(get), None)
    assert "secret-key" not in str(exc.value)


def test_ingest_keeps_first_value(engine, cfg):
    spec = news_spec("fred", title_dedup=False)
    first = fetch(ctx(fake_get(OBS, [])), None)
    revised = fetch(ctx(fake_get([("2026-09-23", "4.99")], [])), None)
    with engine.begin() as conn:
        assert ingest(conn, spec, first, cfg, NOW).rows == 3
        assert ingest(conn, spec, revised, cfg, NOW).rows == 0
        value = conn.execute(
            sa.select(macro_observations.c.value).where(
                macro_observations.c.obs_date == date(2026, 9, 23)
            )
        ).scalar()
    assert value == 4.85
