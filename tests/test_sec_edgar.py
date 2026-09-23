from datetime import UTC, datetime
from types import SimpleNamespace

from tests.conftest import FIXTURES, fake_ctx
from trade_news.collectors.sec_edgar import fetch, form_matches, parse_feed, to_raw_items


def test_parse_8k_feed():
    entries = parse_feed((FIXTURES / "edgar_8k.atom").read_bytes())
    assert len(entries) == 10
    e = entries[0]
    assert e["form"] == "8-K"
    assert e["accession"] and e["cik"] and len(e["cik"]) == 10
    assert e["url"].startswith("https://www.sec.gov/Archives/")
    assert e["updated"].tzinfo is not None
    assert "Item" in e["summary"] and "<" not in e["summary"]


def test_type_filter_is_prefix_so_we_filter_exact_forms():
    entries = parse_feed((FIXTURES / "edgar_type4_mixed.atom").read_bytes())
    forms = {e["form"] for e in entries}
    assert "424B2" in forms  # what the feed really returns for type=4
    kept = [e for e in entries if form_matches(e["form"], "4")]
    assert kept and all(e["form"] in ("4", "4/A") for e in kept)
    assert form_matches("10-K/A", "10-K") and not form_matches("10-KT", "10-K")


def test_form4_issuer_entry_preferred():
    entries = parse_feed((FIXTURES / "edgar_form4.atom").read_bytes())
    items = to_raw_items(entries)
    assert len(items) == len({e["accession"] for e in entries})
    by_acc: dict[str, set] = {}
    for e in entries:
        by_acc.setdefault(e["accession"], set()).add(e["role"])
    for it in items:
        if "Issuer" in by_acc[it.source_item_id]:
            assert it.title.endswith("(Issuer)")


def _feed(*updated: str, form="8-K") -> bytes:
    entries = "".join(
        f"""<entry><title>{form} - Co {i} (000000000{i % 10}) (Filer)</title>
        <link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/{i}"/>
        <summary type="html">x</summary><updated>{u}</updated>
        <category scheme="https://www.sec.gov/" label="form type" term="{form}"/>
        <id>urn:tag:sec.gov,2008:accession-number=0000-26-{i:06d}</id></entry>"""
        for i, u in enumerate(updated)
    )
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode()


def test_fetch_first_run_reads_one_page_and_sets_cursor():
    calls = []

    def get(url, params, headers):
        calls.append(params)
        assert headers["User-Agent"] == "me me@x.com"
        return SimpleNamespace(content=_feed(*["2026-09-23T14:00:00-04:00"] * 100))

    ctx = fake_ctx(get=get, params={"forms": ["8-K"]}, secrets={"SEC_USER_AGENT": "me me@x.com"})
    batch = fetch(ctx, None)
    assert len(calls) == 1
    assert batch.cursor == {"8-K": "2026-09-23T14:00:00-04:00"}
    assert len(batch.items) == 100


def test_fetch_pages_until_known_entries():
    pages = [
        _feed(*["2026-09-23T15:00:00+00:00"] * 100),
        _feed(*(["2026-09-23T14:30:00+00:00"] * 50 + ["2026-09-23T13:00:00+00:00"] * 50)),
        _feed("2026-09-23T12:00:00+00:00"),
    ]

    def get(url, params, headers):
        return SimpleNamespace(content=pages[params["start"] // 100])

    ctx = fake_ctx(
        get=get, params={"forms": ["8-K"], "max_pages": 5}, secrets={"SEC_USER_AGENT": "a b@c.d"}
    )
    batch = fetch(ctx, {"8-K": "2026-09-23T14:00:00+00:00"})
    assert batch.cursor["8-K"] == datetime(2026, 9, 23, 15, tzinfo=UTC).isoformat()
    # stopped on page 2 (it contains entries older than the cursor), never asked for page 3
    assert len(batch.items) == 100  # accessions repeat across the synthetic pages
