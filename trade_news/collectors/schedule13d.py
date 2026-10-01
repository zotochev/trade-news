"""SEC Schedule 13D (beneficial owner of more than 5% with intent to influence): parsing.

Since 2024 Schedule 13D is filed as structured XML (`primary_doc.xml` in the filing folder,
~10-30 KB; the full submission text carries the exhibits and can be megabytes). Per filing:
the issuer, each reporting person with shares and percent of class, the date of the event,
and the items: source of funds (3), purpose of the transaction (4), recent transactions (5c).
Amendments (13D/A) report the new holding, not the change: the previous one is looked up in
our own data (annotation.whale).

Docs: https://www.sec.gov/info/edgar/specifications/form13dxmltechspec (EDGAR Schedule 13D
XML, X0202). Element prefixes vary between filers, so tags are matched without namespaces.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

TEXT_MAX = 600  # chars per item text kept for the LLM and the message


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    cik: str | None
    pct: float | None  # percent of class
    shares: float | None
    types: list[str] = field(default_factory=list)  # IN, CO, PN, IA, HC, ...


@dataclass(frozen=True, slots=True)
class Schedule13D:
    amendment_no: int | None  # None: the initial filing
    issuer_cik: str | None
    issuer_name: str | None
    issuer_state: str | None  # stateOrCountry of the issuer address (US state or country code)
    event_date: str | None  # ISO
    persons: list[Person]
    funds_source: str | None
    purpose: str | None
    transactions: str | None

    @property
    def amendment(self) -> bool:
        return self.amendment_no is not None

    @property
    def pct(self) -> float | None:
        """The group's holding: reporting persons of one filing usually share the same
        shares, so the largest percent is the holding, not the sum."""
        values = [p.pct for p in self.persons if p.pct is not None]
        return max(values) if values else None

    @property
    def filer(self) -> str | None:
        if not self.persons:
            return None
        return max(self.persons, key=lambda p: p.pct or 0).name


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    for node in el.iter():
        if _local(node.tag) == name:
            return node
    return None


def _text(el: ET.Element | None, name: str, limit: int | None = None) -> str | None:
    node = _find(el, name)
    if node is None:
        return None
    text = " ".join("".join(node.itertext()).split())
    if not text:
        return None
    return text if limit is None or len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _float(s: str | None) -> float | None:
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def _date(s: str | None) -> str | None:
    try:
        return datetime.strptime(s, "%m/%d/%Y").date().isoformat() if s else None
    except ValueError:
        return None


def parse(xml: str) -> Schedule13D | None:
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        return None
    kind = _text(root, "submissionType") or ""
    if not kind.startswith("SCHEDULE 13D"):
        return None
    persons = []
    for node in root.iter():
        if _local(node.tag) != "reportingPersonInfo":
            continue
        name = _text(node, "reportingPersonName")
        if not name:
            continue
        persons.append(
            Person(
                name=name,
                cik=_text(node, "reportingPersonCIK"),
                pct=_float(_text(node, "percentOfClass")),
                shares=_float(_text(node, "aggregateAmountOwned")),
                types=[
                    (n.text or "").strip()
                    for n in node.iter()
                    if _local(n.tag) == "typeOfReportingPerson" and (n.text or "").strip()
                ],
            )
        )
    issuer = _find(root, "issuerInfo")
    amendment = _text(root, "amendmentNo")
    return Schedule13D(
        amendment_no=int(amendment)
        if amendment and amendment.isdigit()
        else (0 if kind.endswith("/A") else None),
        issuer_cik=_text(issuer, "issuerCIK"),
        issuer_name=_text(issuer, "issuerName"),
        issuer_state=_text(issuer, "stateOrCountry"),
        event_date=_date(_text(root, "dateOfEvent")),
        persons=persons,
        funds_source=_text(_find(root, "item3"), "fundsSource", TEXT_MAX),
        purpose=_text(_find(root, "item4"), "transactionPurpose", TEXT_MAX),
        transactions=_text(_find(root, "item5"), "transactionDesc", TEXT_MAX),
    )


def _pct(x: float | None) -> str:
    return "?" if x is None else f"{x:g}%"


def headline(s: Schedule13D) -> str:
    form = "13D/A" if s.amendment else "13D"
    return f"Schedule {form} · {s.issuer_name or s.issuer_cik} · {s.filer or '?'} {_pct(s.pct)}"


def describe(s: Schedule13D) -> str:
    form = f"Schedule 13D amendment No. {s.amendment_no}" if s.amendment else "Schedule 13D"
    parts = [
        f"{form}: {s.filer or 'reporting persons'} report {_pct(s.pct)} of "
        f"{s.issuer_name or 'the issuer'}"
        + (f", event date {s.event_date}" if s.event_date else "")
        + "."
    ]
    if s.purpose:
        parts.append(f"Purpose: {s.purpose}")
    if s.transactions:
        parts.append(f"Transactions: {s.transactions}")
    if s.funds_source:
        parts.append(f"Funds: {s.funds_source}")
    return "\n".join(parts)
