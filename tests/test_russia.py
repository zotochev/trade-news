from datetime import UTC, date, datetime
from types import SimpleNamespace

from tests.conftest import NOW, fake_ctx, news_spec
from tests.test_admin import admin  # noqa: F401  (the fixture: admin app over `engine`)
from trade_news import delivery
from trade_news.collectors import moex_dividends, moex_open_positions, ru_minfin
from trade_news.collectors.base import Context
from trade_news.flows.rules import ru_fallback_workday
from trade_news.pipeline import ingest

WORKDAY = ru_fallback_workday([2026, 2027])


def _at(ctx: Context, when: datetime) -> Context:
    return Context(**{f: getattr(ctx, f) for f in Context.__slots__} | {"now": lambda: when})


# --- Minfin ---------------------------------------------------------------------------


def test_minfin_extraction():
    ops = ru_minfin.extract(
        ["Минфин РФ с 7 октября по 6 ноября направит на покупку валюты и золота 279,42 млрд "
         "рублей Ежедневный объем покупок увеличится с 2,5 до 12,7 млрд рублей"], 2026, WORKDAY,
    )  # fmt: skip
    assert ops == {"sign": 1, "daily": 12.7, "total": 279.42,
                   "period": (date(2026, 10, 7), date(2026, 11, 6))}  # fmt: skip
    # no daily figure: total / business days of the period (22)
    ops = ru_minfin.extract(
        ["Минфин с 7 октября по 6 ноября направит на покупку валюты 279,42 млрд рублей"], 2026,
        WORKDAY,
    )  # fmt: skip
    assert ops["daily"] == 12.7
    sale = ru_minfin.extract(
        ["Минфин с 8 декабря по 14 января направит на продажу валюты 30,1 млрд рублей, "
         "1,5 млрд рублей в день"], 2026, WORKDAY,
    )  # fmt: skip
    assert sale["sign"] == -1 and sale["period"][1] == date(2027, 1, 14)
    assert ru_minfin.extract(["Минфин приостановил операции с валютой"], 2026, WORKDAY)["sign"] == 0
    assert ru_minfin.extract(["Минфин разместил ОФЗ на 50 млрд рублей"], 2026, WORKDAY) is None


def test_minfin_collector_polls_only_around_the_announcement(engine, cfg):
    feed = (
        "<rss><channel><item><guid>1</guid><title>Минфин с 7 октября по 6 ноября направит на "
        "покупку валюты 279,42 млрд рублей</title><link>https://tass.ru/1</link>"
        "<description>Ежедневный объем составит 12,7 млрд рублей</description></item>"
        "<item><guid>2</guid><title>Погода в Москве</title></item></channel></rss>"
    ).encode()
    asked = []

    def get(url, **kw):
        asked.append(url)
        return SimpleNamespace(content=feed)

    ctx = fake_ctx(get=get, params={"cbr_mirroring_rub_bn_day": -0.58, "feeds": ["f"]})
    quiet = ru_minfin.fetch(_at(ctx, datetime(2026, 10, 20, 12, tzinfo=UTC)), None)
    assert quiet.items == [] and asked == []  # outside the window: no requests
    found = ru_minfin.fetch(_at(ctx, datetime(2026, 10, 5, 13, tzinfo=UTC)), None)
    (item,) = found.items
    assert item.title == (
        "Минфин: покупка валюты 12,7 млрд руб в день с 07.10 по 06.11; вместе с ЦБ +12,12 "
        "млрд в день"
    )
    assert item.url == "https://tass.ru/1" and item.raw["net_daily_bn"] == 12.12
    (ev,) = found.rows["calendar_events"]
    assert ev["type"] == "ru_minfin_ops" and ev["end_date"] == date(2026, 11, 6) and ev["major"]
    assert "давит на рубль" in ev["details"]
    asked.clear()
    again = ru_minfin.fetch(_at(ctx, datetime(2026, 10, 6, 13, tzinfo=UTC)), found.cursor)
    assert again.items == [] and asked == []  # the month is done
    with engine.begin() as conn:
        ingest(conn, news_spec("ru_minfin", title_dedup=False), found, cfg, NOW)
    # delivered without the importance thresholds
    assert delivery.passes(delivery.DeliveryRules(min_importance=5), [], "other", "ru_minfin")


# --- dividends --------------------------------------------------------------------------


SMARTLAB = """<table><tr><th>Название</th><th>Тикер</th><th>Период</th><th>Дивиденд, руб</th>
<th>Див. Дох.</th><th>СД</th><th>Купить До</th><th>Дата закрытия реестра</th><th>Выплата До</th>
<th>Цена акции</th><th></th></tr>
<tr><td>ЛУКОЙЛ</td><td>LKOH</td><td>3кв 2026</td><td>535</td><td>8,9%</td><td></td>
<td>08.12.2026</td><td>09.12.2026</td><td>19.12.2026</td><td>6002</td><td></td></tr>
<tr><td>Акрон</td><td>AKRN</td><td>2кв 2026</td><td>146</td><td>0,9%</td><td></td>
<td>30.10.2026</td><td>02.11.2026</td><td>12.11.2026</td><td>17140</td><td></td></tr>
<tr><td>MDMG-ао</td><td>MDMG</td><td>2кв 2026</td><td>38</td><td>2,8%</td><td></td>
<td>16.10.2026</td><td>19.10.2026</td><td>29.10.2026</td><td>1341,6</td><td></td></tr>
</table>"""
IMOEX = {"analytics": {"columns": ["ticker", "weight"], "data": [["LKOH", 13.5], ["MDMG", 0.6]]}}


def test_dividend_events_only_index_members():
    rows = moex_dividends.parse_smartlab(SMARTLAB)
    assert rows[0]["dividend"] == 535 and rows[0]["yield"] == 8.9
    ev = moex_dividends.dividend_events(
        rows, moex_dividends.parse_imoex(IMOEX), date(2026, 10, 13), {}
    )
    assert [(e["assets"], e["event_date"], e["major"]) for e in ev] == [
        ("LKOH", date(2026, 12, 8), True),
        ("MDMG", date(2026, 10, 16), False),
    ]  # AKRN is not in IMOEX
    assert ev[0]["key"] == "ru_dividend_cutoff|LKOH|2026-12-09"
    assert ev[0]["title"] == "Последний день с дивидендом LKOH: 535 руб (8.9%)"


# --- open positions -----------------------------------------------------------------------


def _positions(asset, day):
    return {"open_positions": {
        "columns": ["tradedate", "asset", "is_fiz", "persons_long", "persons_short",
                    "open_position_long", "open_position_short", "oichange_long",
                    "oichange_short"],
        "data": [[day, asset, 0, 338, 167, 3523830, 4474565, 14869, -15728],
                 [day, asset, 1, 8679, 4946, 1146573, 195838, -1964, 28633]],
    }}  # fmt: skip


def test_open_positions_recent_days_and_history_walk(engine, cfg, admin):  # noqa: F811
    asked = []

    def get(url, params=None, **kw):
        asset = url.rsplit("/", 1)[1].removesuffix(".json")
        asked.append((asset, params["date"]))
        return SimpleNamespace(json=lambda: _positions(asset, params["date"]))

    params = {"assets": ["Si"], "lookback_days": 3, "backfill_per_run": 4,
              "backfill_from": "2026-09-14"}  # fmt: skip
    ctx = fake_ctx(get=get, params=params)  # NOW: Wednesday 2026-09-23
    first = moex_open_positions.fetch(ctx, None)
    # 23, 22, 21 (recent) + 20 (Sun), 19 (Sat), 18, 17 (history): weekends skipped
    assert [d for _, d in asked] == ["2026-09-23", "2026-09-22", "2026-09-21",
                                     "2026-09-18", "2026-09-17"]  # fmt: skip
    assert first.cursor["backfill_at"] == "2026-09-17"
    asked.clear()
    second = moex_open_positions.fetch(ctx, first.cursor)
    assert [d for _, d in asked][3:] == ["2026-09-16", "2026-09-15", "2026-09-14"]
    spec = news_spec("moex_open_positions", title_dedup=False)
    with engine.begin() as conn:
        assert ingest(conn, spec, first, cfg, NOW).rows == 10
        assert ingest(conn, spec, second, cfg, NOW).rows == 6  # recent days already there
    body = admin.get("/api/ru/open-positions?asset=Si&from=2026-09-01&to=2026-09-30").json()
    assert len(body) == 8 and body[0]["date"] == "2026-09-14"
    assert body[-1]["individuals"]["net"] == 1146573 - 195838
    assert body[-1]["legal"]["persons_long"] == 338
