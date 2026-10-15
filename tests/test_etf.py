from datetime import date
from types import SimpleNamespace

import pytest

from tests.conftest import NOW, fake_ctx, news_spec
from tests.test_admin import admin  # noqa: F401  (the fixture: admin app over `engine`)
from trade_news.collectors import crypto_treasuries, etf_flows
from trade_news.pipeline import ingest

PAGE = """<html><table>
<tr><th></th><th>IBIT</th><th>FBTC</th><th>GBTC</th><th>Total</th></tr>
<tr><td>Fee</td><td>0.25%</td><td>0.25%</td><td>1.50%</td><td></td></tr>
<tr><td>11 Jan 2024</td><td>111.7</td><td>227.0</td><td>(95.1)</td><td>243.6</td></tr>
<tr><td>12 Jan 2024</td><td>1,386.0</td><td>-</td><td>(484.1)</td><td>901.9</td></tr>
<tr><td>15 Jan 2024</td><td>-</td><td>-</td><td>-</td><td>0.0</td></tr>
<tr><td>Total</td><td>1,497</td><td>227</td><td>(579)</td><td>1,145</td></tr>
</table></html>"""


def test_parse_farside_table():
    rows = etf_flows.parse(PAGE, "BTC")
    got = {(r["trade_date"], r["fund"]): r["flow_usd_m"] for r in rows}
    assert got == {
        (date(2024, 1, 11), "IBIT"): 111.7,
        (date(2024, 1, 11), "FBTC"): 227.0,
        (date(2024, 1, 11), "GBTC"): -95.1,
        (date(2024, 1, 12), "IBIT"): 1386.0,
        (date(2024, 1, 12), "GBTC"): -484.1,
    }  # "-" is no data, Fee / Total rows and the Total column are skipped


def test_cloudflare_page_fails_loudly():
    with pytest.raises(ValueError, match="Cloudflare"):
        etf_flows.parse("<html><title>Just a moment...</title></html>", "BTC")
    with pytest.raises(ValueError, match="no table"):
        etf_flows.parse("<html>nothing</html>", "BTC")


def test_collector_sends_a_browser_agent_and_revisions_win(engine, cfg, admin):  # noqa: F811
    pages = {"BTC": PAGE}
    agents = []

    def get(url, headers=None, **kw):
        agents.append(headers["User-Agent"])
        return SimpleNamespace(text=pages["BTC"])

    ctx = fake_ctx(get=get, params={"assets": ["BTC"]})
    spec = news_spec("etf_flows", title_dedup=False)
    with engine.begin() as conn:
        assert ingest(conn, spec, etf_flows.fetch(ctx, None), cfg, NOW).rows == 5
    assert "Chrome" in agents[0]
    # the next morning Farside fills FBTC in for 12 Jan and revises IBIT
    pages["BTC"] = PAGE.replace("<td>1,386.0</td><td>-</td>", "<td>1,380.0</td><td>40.5</td>")
    with engine.begin() as conn:
        assert ingest(conn, spec, etf_flows.fetch(ctx, None), cfg, NOW).rows == 1
    body = admin.get("/api/etf/flows?asset=BTC&from=2024-01-01&to=2024-01-31").json()
    assert body["asset"] == "BTC" and len(body["flows"]) == 6
    assert body["totals"] == [
        {"date": "2024-01-11", "flow_usd_m": 243.6, "funds": 3},
        {"date": "2024-01-12", "flow_usd_m": 936.4, "funds": 3},
    ]
    assert admin.get("/api/etf/flows?asset=DOGE").status_code == 422


def test_treasuries_snapshot_and_api(engine, cfg, admin):  # noqa: F811
    data = {"companies": [
        {"name": "Strategy", "symbol": "MSTR.US", "country": "US", "total_holdings": 848250.0,
         "total_entry_value_usd": 6.45e10, "total_current_value_usd": 6.98e10,
         "percentage_of_total_supply": 4.039},
        {"name": "Metaplanet", "symbol": "3350.T", "country": "JP", "total_holdings": 44000.0},
    ]}  # fmt: skip
    ctx = fake_ctx(
        get=lambda url, **kw: SimpleNamespace(json=lambda: data), params={"coins": ["BTC"]}
    )
    batch = crypto_treasuries.fetch(ctx, None)
    spec = news_spec("crypto_treasuries", title_dedup=False)
    with engine.begin() as conn:
        assert ingest(conn, spec, batch, cfg, NOW).rows == 2
        assert ingest(conn, spec, batch, cfg, NOW).rows == 0  # one snapshot per day
    latest = admin.get(f"/api/treasuries?coin=BTC&to={NOW.date()}").json()
    assert [r["name"] for r in latest] == ["Strategy", "Metaplanet"]
    assert latest[0]["date"] == str(NOW.date()) and latest[0]["pct_supply"] == 4.039
    mstr = admin.get(f"/api/treasuries?coin=BTC&company=MSTR&from=2026-01-01&to={NOW.date()}")
    assert [r["holdings"] for r in mstr.json()] == [848250.0]
