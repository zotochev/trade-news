import io
import zipfile
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from tests.conftest import FIXTURES, NOW
from tests.test_admin import admin  # noqa: F401  (the fixture: admin app over `engine`)
from trade_news import insiders
from trade_news.collectors import form4
from trade_news.db.engine import insert_ignore_many
from trade_news.db.schema import insider_trades, items


def test_form4_rows_carry_holdings_and_every_role():
    f = form4.parse((FIXTURES / "form4_purchase.txt").read_text(encoding="utf-8"))
    (t,) = f.trades
    assert t.shares_after == 54464 and t.direct == "D"
    rows = insiders.rows_from_form4("0001-26-1", asdict(f), NOW, "https://sec/x")
    (r,) = rows
    assert r["ticker"] == "PNRG" and "Director" in r["roles"] and r["code"] == "P"
    assert r["usd"] == 100 * 211.70 and r["shares_after"] == 54464 and r["planned"] is False
    assert r["origin"] == "edgar" and r["transaction_date"] is not None


def _zip(**tables: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, rows in tables.items():
            cols = list(rows[0])
            lines = ["\t".join(cols)] + ["\t".join(str(r[c]) for c in cols) for r in rows]
            z.writestr(f"{name}.tsv", "\n".join(lines) + "\n")
    return buf.getvalue()


def test_load_dataset_skips_known_accessions_and_noise(engine):
    sub = {"ACCESSION_NUMBER": "", "FILING_DATE": "04-NOV-2026", "DOCUMENT_TYPE": "4",
           "ISSUERCIK": "0000350698", "ISSUERNAME": "AutoNation", "ISSUERTRADINGSYMBOL": "an",
           "AFF10B5ONE": "0"}  # fmt: skip
    owner = {"ACCESSION_NUMBER": "", "RPTOWNERNAME": "Manley Mike",
             "RPTOWNER_RELATIONSHIP": "Director,Officer", "RPTOWNER_TITLE": "CEO"}  # fmt: skip
    tx = {"ACCESSION_NUMBER": "", "NONDERIV_TRANS_SK": 2, "TRANS_DATE": "02-NOV-2026",
          "TRANS_CODE": "P", "TRANS_SHARES": "5000", "TRANS_PRICEPERSHARE": "206.5",
          "SHRS_OWND_FOLWNG_TRANS": "90000", "DIRECT_INDIRECT_OWNERSHIP": "D"}  # fmt: skip
    data = _zip(
        SUBMISSION=[sub | {"ACCESSION_NUMBER": "A-1"}, sub | {"ACCESSION_NUMBER": "A-2"},
                    sub | {"ACCESSION_NUMBER": "A-3", "DOCUMENT_TYPE": "3"}],
        REPORTINGOWNER=[owner | {"ACCESSION_NUMBER": a} for a in ("A-1", "A-2", "A-3")],
        NONDERIV_TRANS=[tx | {"ACCESSION_NUMBER": "A-1"},
                        tx | {"ACCESSION_NUMBER": "A-1", "NONDERIV_TRANS_SK": 1, "TRANS_CODE": "M"},
                        tx | {"ACCESSION_NUMBER": "A-2"}, tx | {"ACCESSION_NUMBER": "A-3"}],
    )  # fmt: skip
    with engine.begin() as conn:
        insert_ignore_many(conn, insider_trades, [
            {"accession": "A-2", "seq": 0, "code": "P", "shares": 1.0, "origin": "edgar"}
        ], "accession", "seq")  # fmt: skip
        assert insiders.load_dataset(conn, data) == 1  # A-2 known, A-3 is a Form 3, M dropped
        assert insiders.load_dataset(conn, data) == 0
        (r,) = conn.execute(
            insider_trades.select().where(insider_trades.c.accession == "A-1")
        ).mappings()
    assert r["ticker"] == "AN" and r["roles"] == "CEO, Director" and r["planned"] is False
    assert r["usd"] == 5000 * 206.5 and r["transaction_date"] == date(2026, 11, 2)
    assert r["filed_at"] == datetime(2026, 11, 4, tzinfo=UTC) and r["origin"] == "sec_dataset"
    assert r["url"].endswith("/350698/A1/A-1-index.htm")


def test_dataset_links_newest_first():
    html = ('<a href="/files/x/2026q3_form345.zip">x</a><a href="/files/y/2026q2_form345.zip">'
            '<a href="/files/x/2026q3_form345.zip">')  # fmt: skip
    assert insiders.dataset_links(html) == ["/files/x/2026q3_form345.zip",
                                             "/files/y/2026q2_form345.zip"]  # fmt: skip


def _trade(acc, owner, day, usd, code="P", planned=False, ticker="AN", roles="Director"):
    return {"accession": acc, "seq": 0, "ticker": ticker, "issuer_cik": ticker, "owner": owner,
            "roles": roles, "transaction_date": day, "code": code, "shares": usd / 100,
            "price": 100.0, "usd": usd, "planned": planned, "origin": "edgar",
            "filed_at": datetime.combine(day, datetime.min.time(), UTC) + timedelta(days=2),
            "issuer_name": f"{ticker} Inc"}  # fmt: skip


def test_clusters_window_people_and_plans(engine):
    d = date(2026, 11, 2)  # Monday
    with engine.begin() as conn:
        insert_ignore_many(conn, insider_trades, [
            _trade("1", "CEO A", d, 1_030_000, roles="CEO"),
            _trade("2", "CFO B", d + timedelta(days=4), 500_000, roles="CFO"),
            _trade("3", "CFO B", d + timedelta(days=4), 1_000),  # same person again
            _trade("4", "Dir C", d + timedelta(days=15), 98_000),  # > 10 trading days later
            _trade("5", "Dir D", d, 90_000, planned=True),  # 10b5-1: not a signal
            _trade("6", "Dir E", d, 90_000, code="S"),
            _trade("7", "X", d, 60_000, ticker="ZZ"),
            _trade("8", "Y", d + timedelta(days=1), 30_000, ticker="ZZ"),  # 2 people, < $100K
        ], "accession", "seq")  # fmt: skip
        (c,) = insiders.clusters(conn, d - timedelta(days=5), d + timedelta(days=30))
    assert c["ticker"] == "AN" and c["insiders"] == 2 and c["usd"] == 1_531_000
    assert c["start"] == d and c["end"] == d + timedelta(days=4)
    assert [p["owner"] for p in c["owners"]] == ["CEO A", "CFO B"]
    item = insiders.cluster_item(c)
    assert item.title.startswith("Cluster insider buying · AN · 2 insiders bought $1.53M")
    assert "set importance 5" in item.raw["hint"]  # CEO + CFO, $1.53M
    assert insiders.cluster_importance(c | {"usd": 900_000}) == 4


def test_fresh_clusters_become_news_once(engine, cfg):
    from trade_news.cli import run_insider_clusters

    d = (NOW - timedelta(days=3)).date()
    with engine.begin() as conn:
        insert_ignore_many(conn, insider_trades, [
            _trade("1", "A", d, 200_000) | {"filed_at": NOW - timedelta(hours=5)},
            _trade("2", "B", d, 200_000) | {"filed_at": NOW - timedelta(hours=4)},
        ], "accession", "seq")  # fmt: skip
        assert len(insiders.fresh_cluster_items(conn, NOW)) == 1
        assert insiders.fresh_cluster_items(conn, NOW + timedelta(days=4)) == []

    import trade_news.cli as cli

    cli.utcnow, saved = (lambda: NOW), cli.utcnow
    try:
        run_insider_clusters(engine, cfg)
        run_insider_clusters(engine, cfg)
    finally:
        cli.utcnow = saved
    with engine.connect() as conn:
        rows = conn.execute(items.select().where(items.c.source == "insider_clusters")).all()
    assert len(rows) == 1


def test_insiders_api(admin, engine):  # noqa: F811
    d = date(2026, 9, 21)
    with engine.begin() as conn:
        insert_ignore_many(conn, insider_trades, [
            _trade("1", "CEO A", d, 1_000_000, roles="CEO"),
            _trade("2", "Dir B", d, 150_000),
            _trade("3", "Dir C", d, 50_000, code="S"),
        ], "accession", "seq")  # fmt: skip
    rows = admin.get("/api/insiders?from=2026-09-01&to=2026-09-30").json()
    assert len(rows) == 3 and {r["code"] for r in rows} == {"P", "S"}
    assert rows[0]["direct_or_indirect"] is None and "planned" in rows[0]
    only = admin.get("/api/insiders?from=2026-09-01&to=2026-09-30&code=P&min_usd=200000").json()
    assert [r["owner"] for r in only] == ["CEO A"]
    assert len(admin.get("/api/insiders?from=2026-09-01&to=2026-09-30&role=ceo").json()) == 1
    assert admin.get("/api/insiders?code=X").status_code == 422
    (c,) = admin.get("/api/insiders/clusters?from=2026-09-01&to=2026-09-30").json()
    assert c["insiders"] == 2 and c["usd"] == 1_150_000 and c["owners"][0]["owner"] == "CEO A"


def test_typo_dates_are_dropped():
    filed = datetime(2026, 11, 4, tzinfo=UTC)
    tx = {"code": "P", "shares": 1.0, "price": 2.0}
    row = insiders._row("A", 0, {}, [], [], None, filed, None, "edgar", tx | {"date": "2026-11-02"})
    assert row["transaction_date"] == date(2026, 11, 2)
    for typo in ("0026-11-02", "2028-05-24"):
        row = insiders._row("A", 0, {}, [], [], None, filed, None, "edgar", tx | {"date": typo})
        assert row["transaction_date"] is None
