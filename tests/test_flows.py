from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from tests.conftest import NOW, fake_ctx, news_spec
from tests.test_admin import admin  # noqa: F401  (the fixture: admin app over `engine`)
from trade_news import delivery
from trade_news.collectors import finnhub_calendar, flow_rules, treasury_auctions
from trade_news.db.schema import calendar_events, rate_expectations, token_unlocks
from trade_news.flows import calendar, notify, rules
from trade_news.pipeline import ingest


def _by(events, type_):
    return [e["event_date"] for e in events if e["type"] == type_]


# --- rules ------------------------------------------------------------------------------


def test_us_rules_follow_exchange_holidays():
    ev = rules.us_events(date(2026, 10, 1), date(2027, 12, 31))
    # Juneteenth 2027 is observed on Friday 18.06: the quarterly expiry moves to Thursday,
    # and VIX expires 30 days before that Thursday (a Tuesday)
    assert date(2027, 6, 17) in _by(ev, "opex_quarterly")
    assert date(2027, 5, 18) in _by(ev, "vix_expiry")
    assert date(2026, 10, 16) in _by(ev, "opex_monthly")
    assert date(2026, 10, 21) in _by(ev, "vix_expiry")  # 30 days before 20.11
    # Russell: 2nd Friday of December, 4th Friday of June
    assert _by(ev, "russell_recon") == [date(2026, 12, 11), date(2027, 6, 25), date(2027, 12, 10)]
    assert date(2026, 11, 30) in _by(ev, "index_rebalance")  # MSCI, last business day of Nov
    assert date(2027, 4, 30) in _by(ev, "month_end")
    assert date(2026, 12, 31) in _by(ev, "quarter_end")
    keys = [e["key"] for e in ev]
    assert len(keys) == len(set(keys))


def test_ru_rules_use_the_production_calendar():
    workday = rules.ru_fallback_workday([2026, 2027])
    ev = rules.ru_events(date(2026, 11, 1), date(2027, 2, 28), workday)
    # November: 2, 3 are workdays, 4 is a holiday -> 3rd workday is Thursday 5.11
    assert date(2026, 11, 5) in _by(ev, "ru_minfin_announce")
    assert date(2027, 1, 13) in _by(ev, "ru_minfin_announce")  # after the New Year holidays
    assert date(2027, 2, 1) not in _by(ev, "ru_tax_period")
    assert date(2027, 3, 1) not in _by(ev, "ru_tax_period")  # beyond the range
    assert date(2026, 11, 30) in _by(ev, "ru_tax_period")  # 28.11 is a Saturday


def test_halving_estimate():
    e = rules.halving(1_049_856, date(2026, 10, 13))  # 144 blocks left = 1 day
    assert e["event_date"] == date(2026, 10, 14) and e["key"] == "btc_halving|1050000"
    assert e["precision"] == "estimated" and e["major"]


# --- collectors ---------------------------------------------------------------------------


def _isdayoff_get(url, **kw):
    if "isdayoff" in url:
        y = kw["params"]["year"]
        n = (date(y + 1, 1, 1) - date(y, 1, 1)).days
        # every day a workday except Sundays; 2026-11-03 (a Tuesday) is a day off
        days = [
            "1"
            if date(y, 1, 1) + timedelta(d) in (date(2026, 11, 3),)
            or (date(y, 1, 1) + timedelta(d)).weekday() == 6
            else "0"
            for d in range(n)
        ]
        return SimpleNamespace(text="".join(days))  # fmt: skip
    return SimpleNamespace(text="1049000")


def test_flow_rules_collector_upserts_and_keeps_edits(engine, cfg):
    ctx = fake_ctx(get=_isdayoff_get, params={"horizon_days": 90})
    batch = flow_rules.fetch(ctx, None)
    ev = batch.rows["calendar_events"]
    # the production calendar from isdayoff: Saturdays are workdays here, 03.11 is not
    assert date(2026, 10, 3) in _by(ev, "ru_minfin_announce")
    assert date(2026, 11, 5) in _by(ev, "ru_minfin_announce")  # 1st Sun, 3rd off
    assert "2026" in batch.cursor["ru_days"]
    spec = news_spec("flow_rules", title_dedup=False)
    with engine.begin() as conn:
        n = ingest(conn, spec, batch, cfg, NOW).rows
        assert n == len(ev)
        conn.execute(
            calendar_events.update()
            .where(calendar_events.c.type == "opex_monthly")
            .values(title="Правка", edited=True)
        )
    again = flow_rules.fetch(ctx, batch.cursor)  # cursor is fresh: no isdayoff request
    with engine.begin() as conn:
        assert ingest(conn, spec, again, cfg, NOW).rows == 0  # no duplicates
        titles = conn.execute(
            sa.select(calendar_events.c.title).where(calendar_events.c.type == "opex_monthly")
        ).scalars()
        assert set(titles) == {"Правка"}


def test_finnhub_calendar_asks_week_by_week():
    asked = []

    def get(url, params=None, **kw):
        asked.append((url.rsplit("/", 1)[1], params["from"]))
        if url.endswith("/earnings"):
            rows = [
                {"symbol": "JPM", "date": "2026-10-13", "hour": "bmo", "quarter": 3, "year": 2026},
                {"symbol": "TINY", "date": "2026-10-13", "hour": "", "quarter": 3, "year": 2026},
            ] if params["from"] == "2026-10-13" else []  # fmt: skip
            return SimpleNamespace(json=lambda: {"earningsCalendar": rows})
        ipos = [
            {"date": "2026-04-17", "name": "Big IPO", "symbol": "BIG", "status": "priced",
             "totalSharesValue": 5_000_000_000},
            {"date": "2026-04-17", "name": "Mid", "symbol": "MID", "status": "priced",
             "totalSharesValue": 625_000_000},
            {"date": "2026-04-17", "name": "Rich Acquisition Corp", "symbol": "RICH",
             "status": "priced", "totalSharesValue": 300_000_000},
            {"date": "2026-04-17", "name": "Unit Co", "symbol": "SPCXU", "status": "priced",
             "totalSharesValue": 300_000_000},
            {"date": "2026-04-17", "name": "Small", "symbol": "SML", "status": "priced",
             "totalSharesValue": 50_000_000},
        ]  # fmt: skip
        return SimpleNamespace(json=lambda: {"ipoCalendar": ipos})

    ctx = fake_ctx(get=get, params={"symbols": ["JPM"], "earnings_days": 20},
                   secrets={"FINNHUB_API_KEY": "k"})  # fmt: skip
    ctx = ctx.__class__(**{**{f: getattr(ctx, f) for f in ctx.__slots__},
                           "now": lambda: datetime(2026, 10, 13, 6, tzinfo=UTC)})  # fmt: skip
    ev = finnhub_calendar.fetch(ctx, None).rows["calendar_events"]
    assert [a for a in asked if a[0] == "earnings"] == [
        ("earnings", "2026-10-13"), ("earnings", "2026-10-20"), ("earnings", "2026-10-27"),
    ]  # fmt: skip
    keys = {e["key"]: e for e in ev}
    assert keys["earnings|JPM|2026-Q3"]["event_time"] == "09:00"
    blackout = keys["buyback_blackout|JPM|2026-Q3"]
    assert blackout["event_date"] == date(2026, 9, 15) and blackout["end_date"] == date(
        2026, 10, 13
    )
    assert keys["ipo_lockup|BIG"]["event_date"] == date(2026, 10, 14)
    assert keys["ipo_lockup|BIG"]["major"] and not keys["ipo_lockup|MID"]["major"]
    assert "ipo_lockup|SML" not in keys  # below min_ipo_usd
    assert "ipo_lockup|RICH" not in keys and "ipo_lockup|SPCXU" not in keys  # SPACs
    assert not any("TINY" in k for k in keys)


def test_treasury_auctions():
    rows = [
        {"cusip": "A", "securityType": "Bill", "securityTerm": "13-Week",
         "auctionDate": "2026-11-09T00:00:00", "offeringAmount": "95000000000"},
        {"cusip": "B", "securityType": "Note", "securityTerm": "10-Year",
         "auctionDate": "2026-11-10T00:00:00", "offeringAmount": "42000000000",
         "closingTimeCompetitive": "01:00 PM", "reopening": "No"},
        {"cusip": "C", "securityType": "Note", "securityTerm": "5-Year",
         "auctionDate": "2026-10-22T00:00:00", "offeringAmount": ""},
    ]  # fmt: skip
    ev = treasury_auctions.auction_events(rows, include_bills=False)
    ten, five = ev
    assert ten["major"] and ten["event_time"] == "13:00" and ten["size"] == 42e9
    assert (
        "refunding" in ten["details"] and ten["title"] == "Аукцион Treasuries 10-Year на $42000.0M"
    )
    assert not five["major"] and five["size"] is None and "не объявлен" in five["details"]


# --- reading, API, admin, Telegram -------------------------------------------------------


def _fill(engine):
    with engine.begin() as conn:
        conn.execute(calendar_events.insert(), [
            rules.event("opex_monthly", "us_equity", date(2026, 9, 25), "Экспирация", "d",
                        time="16:00", tz=rules.NY) | {"updated_at": NOW},
            rules.event("quarter_end", "us_equity", date(2026, 9, 30), "Конец квартала", "d",
                        major=True) | {"updated_at": NOW},
            rules.event("buyback_blackout", "us_equity", date(2026, 9, 10), "Тихий AAPL", "d",
                        assets="AAPL", end=date(2026, 10, 8)) | {"updated_at": NOW},
            rules.event("earnings", "us_equity", date(2026, 9, 24), "Отчёт ORCL", "d",
                        assets="ORCL") | {"updated_at": NOW},
        ])  # fmt: skip
        conn.execute(token_unlocks.insert().values(
            protocol="arbitrum", unlock_at=datetime(2026, 9, 26, 14, tzinfo=UTC), name="Arbitrum",
            coin="coingecko:arbitrum", tokens=9e7, pct_unlocked=6.2, recipients="Team",
            fetched_at=NOW,
        ))  # fmt: skip
        conn.execute(rate_expectations.insert().values(
            central_bank="Fed", meeting_date=date(2026, 9, 30), snapshot_at=NOW,
            outcome="350-375", probability=90.0,
        ))  # fmt: skip


def test_calendar_merges_sources(engine):
    _fill(engine)
    with engine.connect() as conn:
        ev = calendar.events(conn, date(2026, 9, 21), date(2026, 9, 30))
        major = calendar.events(conn, date(2026, 9, 21), date(2026, 9, 30), major_only=True)
    assert [e["type"] for e in ev] == [
        "buyback_blackout", "earnings", "opex_monthly", "token_unlock", "quarter_end",
        "cb_meeting",
    ]  # fmt: skip
    assert {e["type"] for e in major} == {"token_unlock", "cb_meeting", "quarter_end"}


def test_calendar_api(admin, engine):  # noqa: F811
    _fill(engine)
    resp = admin.get("/api/calendar?from=2026-09-21&to=2026-09-30&markets=crypto,us_rates")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["type"] for e in body] == ["token_unlock", "cb_meeting"]
    assert body[0]["assets"] == ["Arbitrum"] and body[0]["size_unit"] == "pct_supply"
    assert admin.get("/api/calendar?from=2026-09-30&to=2026-09-01").status_code == 422


def test_calendar_page_adds_and_edits(admin, engine):  # noqa: F811
    form = {"event_date": "2026-12-18", "type": "index_change", "market": "us_equity",
            "title": "S&P 500: включение XYZ", "assets": "XYZ, ", "major": "1",
            "event_time": "16:00", "tz": "America/New_York"}  # fmt: skip
    assert admin.post("/calendar/save", data=form, follow_redirects=False).status_code == 303
    with engine.connect() as conn:
        row = conn.execute(sa.select(calendar_events)).mappings().one()
    assert row["source"] == "manual" and row["assets"] == "XYZ" and row["major"]
    page = admin.get("/calendar?weeks=8").text  # NOW is 23.09: 18.12 is beyond 8 weeks
    assert "S&amp;P 500: включение XYZ" not in page
    edit = admin.get(f"/calendar?edit={row['id']}").text
    assert 'value="S&amp;P 500: включение XYZ"' in edit and "Удалить" in edit
    admin.post("/calendar/save", data=form | {"id": str(row["id"]), "event_date": "2026-10-01"})
    assert "S&amp;P 500: включение XYZ" in admin.get("/calendar").text
    admin.post(f"/calendar/{row['id']}/delete")
    assert "Событий по фильтру нет" in admin.get("/calendar").text


def test_digest_and_reminders(engine):
    _fill(engine)
    with engine.begin() as conn:
        delivery.save_rules(conn, delivery.DeliveryRules(enabled=True), NOW)
    text = "\n".join(notify.build_digest(engine, date(2026, 9, 23)))
    assert "21.09 – 27.09" in text and "Тихий период выкупа у 1 крупных компаний" in text
    assert "<b>Чт 24.09</b>\n• Отчёты: ORCL" in text
    assert "• 16:00 NY · Экспирация — маркетмейкеры снимают хеджи" in text
    assert "<b>Разлок Arbitrum: 6.2% предложения" in text and "Конец квартала" not in text

    sent = []
    api = lambda method, **kw: sent.append(kw["text"])  # noqa: E731
    friday = lambda: datetime(2026, 9, 25, 15, tzinfo=UTC)  # noqa: E731
    assert notify.run_reminders(engine, api, 1, friday) == 1  # Sat..Mon: only the unlock
    assert "Разлок Arbitrum" in sent[0] and "Конец квартала" not in sent[0]
    assert notify.run_reminders(engine, api, 1, friday) == 0  # already reminded
    tuesday = lambda: datetime(2026, 9, 29, 15, tzinfo=UTC)  # noqa: E731
    assert notify.run_reminders(engine, api, 1, tuesday) == 1
    assert "Конец квартала" in sent[1] and "Решение ФРС" in sent[1]
