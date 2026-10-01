"""Schedule 13D: parsing the structured XML and fetching it in the EDGAR collector."""

from types import SimpleNamespace

from tests.conftest import FIXTURES, fake_ctx
from trade_news.collectors import schedule13d
from trade_news.collectors.sec_edgar import fetch

INITIAL = (FIXTURES / "schedule13d.xml").read_text(encoding="utf-8")  # default namespace
AMENDMENT = (FIXTURES / "schedule13da.xml").read_text(encoding="utf-8")  # "sch:" prefixes


def test_parse_initial_filing():
    s = schedule13d.parse(INITIAL)
    assert s.amendment_no is None and not s.amendment
    assert s.issuer_cik == "0001846416" and s.issuer_name.startswith("ONE Nuclear Energy")
    assert s.issuer_state == "FL" and s.event_date == "2026-09-23"
    assert len(s.persons) == 5 and s.pct == 4.6  # a group shares one holding: max, not sum
    assert s.filer == "NCCS Management, LLC"
    assert s.purpose.startswith("The Fund acquired the securities") and len(s.purpose) <= 600
    assert schedule13d.headline(s).startswith("Schedule 13D · ONE Nuclear Energy")
    assert "report 4.6% of ONE Nuclear Energy" in schedule13d.describe(s)


def test_parse_amendment_with_prefixes():
    s = schedule13d.parse(AMENDMENT)
    assert s.amendment_no == 4 and s.amendment
    assert s.issuer_name == "Global Business Travel Group, Inc." and s.pct == 0.0
    assert s.filer == "American Express Company"
    assert schedule13d.headline(s) == (
        "Schedule 13D/A · Global Business Travel Group, Inc. · American Express Company 0%"
    )


def test_parse_rejects_other_documents():
    assert schedule13d.parse("<edgarSubmission><submissionType>4</submissionType>"
                             "</edgarSubmission>") is None  # fmt: skip
    assert schedule13d.parse("not xml") is None


def _entry(acc: str, role: str, form: str = "SCHEDULE 13D") -> str:
    url = f"https://www.sec.gov/Archives/edgar/data/1/{acc.replace('-', '')}/{acc}-index.htm"
    name = "ONE Nuclear Energy Inc." if role == "Subject" else "NCCS Management, LLC"
    return f"""<entry><title>{form} - {name} (0001846416) ({role})</title>
      <link rel="alternate" type="text/html" href="{url}"/>
      <summary type="html">x</summary><updated>2026-10-01T09:00:00-04:00</updated>
      <category scheme="https://www.sec.gov/" label="form type" term="{form}"/>
      <id>urn:tag:sec.gov,2008:accession-number={acc}</id></entry>"""


def test_collector_fetches_13d_once_and_keeps_the_company_entry():
    feed = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + _entry("0000000001-26-000001", "Filed by")
        + _entry("0000000001-26-000001", "Subject")
        + _entry("0000000001-26-000002", "Subject", "SCHEDULE 13D/A")
        + "</feed>"
    )
    docs = {"000000000126000001": INITIAL, "000000000126000002": AMENDMENT}
    fetched = []

    def get(url, params=None, headers=None):
        if url.endswith("primary_doc.xml"):
            folder = url.rsplit("/", 2)[1]
            fetched.append(folder)
            return SimpleNamespace(text=docs[folder])
        return SimpleNamespace(content=feed.encode())

    ctx = fake_ctx(
        get=get, params={"forms": ["SCHEDULE 13D"]}, secrets={"SEC_USER_AGENT": "a b@c.d"}
    )
    batch = fetch(ctx, {"SCHEDULE 13D": "2026-10-01T08:00:00-04:00"})
    by_id = {it.source_item_id: it for it in batch.items}
    assert sorted(fetched) == sorted(docs)  # only primary_doc.xml, once per filing
    first = by_id["0000000001-26-000001"]
    assert first.raw["role"] == "Subject" and first.raw["schedule13d"]["issuer_cik"] == "0001846416"
    assert first.title.startswith("Schedule 13D · ONE Nuclear Energy")
    assert by_id["0000000001-26-000002"].raw["schedule13d"]["amendment_no"] == 4
    assert len(batch.cursor["13d_seen"]) == 2
    again = fetch(ctx, batch.cursor)  # the feed repeats them: nothing re-sent, nothing refetched
    assert again.items == [] and len(fetched) == 2


def test_newly_added_form_backlog_is_not_annotated():
    feed = '<feed xmlns="http://www.w3.org/2005/Atom">' + _entry("0000000001-26-000001", "Subject")
    feed += "</feed>"

    def get(url, params=None, headers=None):
        if url.endswith("primary_doc.xml"):
            return SimpleNamespace(text=INITIAL)
        return SimpleNamespace(content=feed.encode())

    ctx = fake_ctx(
        get=get, params={"forms": ["4", "SCHEDULE 13D"]}, secrets={"SEC_USER_AGENT": "a b@c.d"}
    )
    # the source ran before (Form 4 cursor) but 13D is new: its first page is a backlog
    batch = fetch(ctx, {"4": "2026-10-01T08:00:00-04:00"})
    (item,) = batch.items
    assert item.raw["llm_skip"] == "first run backlog" and "schedule13d" in item.raw
