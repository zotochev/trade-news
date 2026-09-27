"""Data for the admin "Рынок" page: what the annotated news flow and our macro data say.

No prices yet (stage 5), so the page shows attention and tone, not moves:
- sector map: items per industry per day with the net tone of their main links;
- attention leaders: assets written about most in 24 h against their usual daily count;
- macro: Treasury yields, the Fed funds rate, CPI / payrolls / unemployment (FRED), FedWatch
  probabilities for the next meetings, the high-impact calendar ahead;
- insiders: the Form 4 trades that passed our thresholds.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import sqlalchemy as sa

from trade_news import sectors, views
from trade_news.db.schema import (
    annotations,
    assets,
    econ_events,
    item_assets,
    items,
    macro_observations,
    rate_expectations,
    raw_items,
)

SIGN = {"bullish": 1, "bearish": -1}
DAYS = 7


def _main(links: list[dict]) -> dict | None:
    primary = [x for x in links if x.get("is_primary")] or links
    return max(primary, key=lambda x: x.get("importance") or 0) if primary else None


def _annotated_items(conn, since: datetime) -> list[dict]:
    rows = conn.execute(
        sa.select(items.c.id, items.c.published_at)
        .join(annotations, sa.and_(annotations.c.item_id == items.c.id, views.current_annotation()))
        .where(items.c.published_at >= since)
    ).all()
    return [{"id": r.id, "at": r.published_at} for r in rows]


# --- sector map -----------------------------------------------------------------------


@dataclass
class Cell:
    count: int = 0
    bull: int = 0
    bear: int = 0
    score: int = 0  # sum of ±importance of the main links

    @property
    def tone(self) -> float:
        """-1 … +1: net share of directional news, weighted by importance."""
        return max(-1.0, min(1.0, self.score / (self.count * 4))) if self.count else 0.0

    @property
    def fill_pct(self) -> int:
        """How much of the bull/bear color goes into the cell background (the rest: neutral)."""
        return round(abs(self.tone) * 70)

    def add(self, main: dict | None) -> None:
        self.count += 1
        direction = (main or {}).get("direction")
        self.bull += direction == "bullish"
        self.bear += direction == "bearish"
        self.score += SIGN.get(direction, 0) * ((main or {}).get("importance") or 0)


@dataclass
class SectorRow:
    key: str
    name: str
    days: dict[date, Cell] = field(default_factory=lambda: defaultdict(Cell))
    last24: Cell = field(default_factory=Cell)
    prev24: Cell = field(default_factory=Cell)
    week: Cell = field(default_factory=Cell)
    industries: list[SectorRow] = field(default_factory=list)


def sector_map(conn, now: datetime) -> tuple[list[date], list[SectorRow]]:
    """(days, sectors with their industries), sectors by weekly count."""
    days = [(now - timedelta(days=i)).date() for i in range(DAYS - 1, -1, -1)]
    found = _annotated_items(conn, now - timedelta(days=DAYS))
    ids = [f["id"] for f in found]
    links, secs = views.links_by_item(conn, ids), views.sectors_by_item(conn, ids)
    rows: dict[str, SectorRow] = {}
    for f in found:
        main = _main(links.get(f["id"], []))
        keys = set()
        for sector, industry in secs.get(f["id"], []):
            keys.add(sector)
            if industry:
                keys.add(industry)
        for key in keys:
            row = rows.setdefault(key, SectorRow(key, sectors.name_ru(key)))
            row.days[f["at"].date()].add(main)
            row.week.add(main)
            if f["at"] >= now - timedelta(hours=24):
                row.last24.add(main)
            elif f["at"] >= now - timedelta(hours=48):
                row.prev24.add(main)
    out = []
    for s in sectors.SECTORS:
        if s.key not in rows:
            continue
        row = rows[s.key]
        row.industries = sorted(
            (rows[i.key] for i in s.industries if i.key in rows), key=lambda r: -r.week.count
        )
        out.append(row)
    return days, sorted(out, key=lambda r: -r.week.count)


# --- attention leaders ----------------------------------------------------------------


@dataclass
class Leader:
    asset_id: int
    symbol: str
    name: str | None
    sector: str | None
    industry: str | None
    last24: Cell = field(default_factory=Cell)
    before: int = 0  # items in the rest of the week
    ratio: float | None = None  # last24 / usual daily count; None: no history yet


def attention(conn, now: datetime, limit: int = 15) -> list[Leader]:
    week_ago = now - timedelta(days=DAYS)
    day_ago = now - timedelta(hours=24)
    rows = conn.execute(
        sa.select(
            item_assets.c.item_id, item_assets.c.asset_id, item_assets.c.direction,
            item_assets.c.importance, items.c.published_at, assets.c.symbol, assets.c.name,
            assets.c.sector, assets.c.industry,
        )
        .join(items, items.c.id == item_assets.c.item_id)
        .join(assets, assets.c.id == item_assets.c.asset_id)
        .where(
            items.c.published_at >= week_ago,
            item_assets.c.annotation_id.in_(views.latest_annotation_ids()),
            sa.or_(item_assets.c.is_primary.is_(True), item_assets.c.importance >= 4),
        )
    ).all()  # fmt: skip
    first = conn.execute(sa.select(sa.func.min(items.c.published_at))).scalar() or now
    history_days = max(0.0, (day_ago - max(week_ago, first)).total_seconds() / 86400)
    leaders: dict[int, Leader] = {}
    seen: set[tuple[int, int]] = set()
    for r in rows:
        if (r.item_id, r.asset_id) in seen:
            continue
        seen.add((r.item_id, r.asset_id))
        ld = leaders.setdefault(
            r.asset_id, Leader(r.asset_id, r.symbol, r.name, r.sector, r.industry)
        )
        if r.published_at >= day_ago:
            ld.last24.add({"direction": r.direction, "importance": r.importance})
        else:
            ld.before += 1
    top = sorted(
        (ld for ld in leaders.values() if ld.last24.count),
        key=lambda ld: (-ld.last24.count, -ld.last24.score, ld.symbol),
    )[:limit]
    for ld in top:
        if history_days >= 1 and ld.before:
            ld.ratio = ld.last24.count / (ld.before / history_days)
    return top


# --- macro ------------------------------------------------------------------------------


@dataclass
class Macro:
    yields: list[dict]  # [{"d": "2026-09-24", "y2": 3.52, "y10": 4.11}] for the chart
    tiles: list[dict]  # {"label", "value", "change", "note"}
    fedwatch: list[dict]  # [{"meeting": date, "outcomes": [(outcome, prob, change)]}]
    fedwatch_at: datetime | None
    calendar: list[dict]


def _series(conn, series_id: str, since: date | None = None) -> list[tuple[date, float]]:
    q = sa.select(macro_observations.c.obs_date, macro_observations.c.value).where(
        macro_observations.c.series_id == series_id
    )
    if since:
        q = q.where(macro_observations.c.obs_date >= since)
    return [(r.obs_date, r.value) for r in conn.execute(q.order_by(macro_observations.c.obs_date))]


def _yoy(points: list[tuple[date, float]]) -> tuple[date, float] | None:
    if not points:
        return None
    last_d, last_v = points[-1]
    year_ago = {d: v for d, v in points}.get(last_d.replace(year=last_d.year - 1))
    return (last_d, (last_v / year_ago - 1) * 100) if year_ago else None


def macro(conn, now: datetime) -> Macro:
    today = now.date()
    y2 = _series(conn, "DGS2", today - timedelta(days=366))
    y10 = _series(conn, "DGS10", today - timedelta(days=366))
    by_day: dict[date, dict] = defaultdict(dict)
    for d, v in y2:
        by_day[d]["y2"] = v
    for d, v in y10:
        by_day[d]["y10"] = v
    yields = [
        {"d": d.isoformat(), "y2": v["y2"], "y10": v["y10"]}
        for d, v in sorted(by_day.items())
        if "y2" in v and "y10" in v
    ]
    tiles = []

    def level(label: str, points, unit="%", bp=True):
        if not points:
            return
        d, v = points[-1]
        change = None
        if len(points) > 1:
            diff = v - points[-2][1]
            change = f"{diff * 100:+.0f} б.п." if bp else f"{diff:+.2f}"
        tiles.append({"label": label, "value": f"{v:.2f}{unit}", "change": change,
                      "note": f"{d:%d.%m}"})  # fmt: skip

    level("US 2Y", y2)
    level("US 10Y", y10)
    if yields:
        spread = (yields[-1]["y10"] - yields[-1]["y2"]) * 100
        prev = (yields[-2]["y10"] - yields[-2]["y2"]) * 100 if len(yields) > 1 else None
        tiles.append({
            "label": "Спред 2s10s", "value": f"{spread:+.0f} б.п.",
            "change": f"{spread - prev:+.0f} б.п." if prev is not None else None,
            "note": "10Y − 2Y",
        })  # fmt: skip
    level("Ставка ФРС (EFFR)", _series(conn, "DFF", today - timedelta(days=30)))
    for sid, label in (("CPIAUCSL", "CPI г/г"), ("CPILFESL", "Базовый CPI г/г")):
        if yoy := _yoy(_series(conn, sid)):
            tiles.append({"label": label, "value": f"{yoy[1]:.1f}%", "change": None,
                          "note": f"за {yoy[0]:%m.%Y}"})  # fmt: skip
    unrate = _series(conn, "UNRATE")
    if unrate:
        d, v = unrate[-1]
        change = f"{v - unrate[-2][1]:+.1f} п.п." if len(unrate) > 1 else None
        tiles.append({"label": "Безработица", "value": f"{v:.1f}%", "change": change,
                      "note": f"за {d:%m.%Y}"})  # fmt: skip
    payems = _series(conn, "PAYEMS")
    if len(payems) > 1:
        d, v = payems[-1]
        tiles.append({"label": "Занятость (NFP) м/м", "value": f"{v - payems[-2][1]:+.0f} тыс.",
                      "change": None, "note": f"за {d:%m.%Y}"})  # fmt: skip
    fw, fw_at = _fedwatch(conn)
    return Macro(yields, tiles, fw, fw_at, _calendar(conn, now))


def _fedwatch(conn) -> tuple[list[dict], datetime | None]:
    snaps = list(
        conn.execute(
            sa.select(rate_expectations.c.snapshot_at)
            .distinct()
            .order_by(rate_expectations.c.snapshot_at.desc())
            .limit(2)
        ).scalars()
    )
    if not snaps:
        return [], None

    def load(at):
        rows = conn.execute(
            sa.select(rate_expectations).where(rate_expectations.c.snapshot_at == at)
        ).all()
        return {(r.meeting_date, r.outcome): r.probability for r in rows}

    last = load(snaps[0])
    prev = load(snaps[1]) if len(snaps) > 1 else {}
    meetings = sorted({m for m, _ in last})[:3]
    out = []
    for m in meetings:
        outcomes = sorted((o, p) for (mm, o), p in last.items() if mm == m)
        out.append({
            "meeting": date.fromisoformat(str(m)[:10]),
            "outcomes": [
                (o, p, p - prev[(m, o)] if (m, o) in prev else None)
                for o, p in outcomes if p >= 1
            ],
        })  # fmt: skip
    return out, snaps[0]


def _calendar(conn, now: datetime, days: int = 7, limit: int = 12) -> list[dict]:
    """High-impact events ahead, the latest snapshot of each."""
    at = econ_events.c.scheduled_at
    latest = (
        sa.select(sa.func.max(econ_events.c.id))
        .where(at >= now, at < now + timedelta(days=days))
        .group_by(econ_events.c.event_name, econ_events.c.currency, at)
    )
    rows = conn.execute(
        sa.select(econ_events)
        .where(econ_events.c.id.in_(latest), econ_events.c.importance >= 3)
        .order_by(econ_events.c.scheduled_at)
        .limit(limit)
    ).mappings()
    return [dict(r) for r in rows]


# --- insiders ---------------------------------------------------------------------------


def insiders(conn, now: datetime, days: int = DAYS, limit: int = 20) -> list[dict]:
    """Form 4 trades above our thresholds (the ones sent to the LLM)."""
    rows = conn.execute(
        sa.select(items.c.id, items.c.published_at, raw_items.c.raw_json)
        .join(raw_items, raw_items.c.id == items.c.raw_item_id)
        .where(items.c.source == "sec_edgar", items.c.published_at >= now - timedelta(days=days))
        .order_by(items.c.published_at.desc())
    ).all()
    out = []
    for r in rows:
        raw = r.raw_json or {}
        f4 = raw.get("form4")
        if not f4 or raw.get("llm_skip") or not raw.get("significance"):
            continue
        codes = Counter(t.get("code") for t in f4.get("trades") or [])
        out.append({
            "id": r.id,
            "at": r.published_at,
            "ticker": f4.get("ticker") or "",
            "issuer": f4.get("issuer_name") or "",
            "owners": ", ".join(f4.get("owners") or []),
            "roles": ", ".join(f4.get("roles") or []),
            "buy": codes["P"] > codes["S"],
            "what": str(raw["significance"]),
            "planned": bool(f4.get("planned")),
        })  # fmt: skip
        if len(out) >= limit:
            break
    return out


# --- yields chart geometry (inline SVG, no JS library) -------------------------------------

CHART_W, CHART_H = 720, 240
PAD_L, PAD_R, PAD_T, PAD_B = 44, 84, 12, 26


def yield_chart(points: list[dict]) -> dict | None:
    """Paths, ticks and per-point x for the 2Y/10Y line chart; None without data."""
    if len(points) < 2:
        return None
    values = [p[k] for p in points for k in ("y2", "y10")]
    lo, hi = min(values), max(values)
    step = 0.25 if hi - lo <= 1.5 else 0.5
    lo = step * int(lo / step) - (step if lo % step == 0 else 0)
    hi = step * (int(hi / step) + 1)
    n = len(points)
    plot_w, plot_h = CHART_W - PAD_L - PAD_R, CHART_H - PAD_T - PAD_B

    def x(i):
        return round(PAD_L + plot_w * i / (n - 1), 1)

    def y(v):
        return round(PAD_T + plot_h * (hi - v) / (hi - lo), 1)

    def path(key):
        return "M" + " L".join(f"{x(i)},{y(p[key])}" for i, p in enumerate(points))

    y_ticks = []
    v = lo
    while v <= hi + 1e-9:
        y_ticks.append({"y": y(v), "label": f"{v:.2f}".rstrip("0").rstrip(".")})
        v += step
    x_ticks, seen = [], set()
    for i, p in enumerate(points):
        month = p["d"][:7]
        if month not in seen and p["d"][8:10] <= "07":
            seen.add(month)
            x_ticks.append({"x": x(i), "label": f"{p['d'][5:7]}.{p['d'][2:4]}"})
    last = points[-1]
    ly2, ly10 = y(last["y2"]), y(last["y10"])
    if abs(ly2 - ly10) < 14:  # keep the end labels apart
        mid = (ly2 + ly10) / 2
        up, down = mid - 7, mid + 7
        ly2, ly10 = (down, up) if last["y2"] <= last["y10"] else (up, down)
    return {
        "w": CHART_W, "h": CHART_H, "left": PAD_L, "right": CHART_W - PAD_R,
        "top": PAD_T, "bottom": CHART_H - PAD_B,
        "p2": path("y2"), "p10": path("y10"),
        "end2": {"x": x(n - 1), "y": y(last["y2"]), "ly": ly2, "v": last["y2"]},
        "end10": {"x": x(n - 1), "y": y(last["y10"]), "ly": ly10, "v": last["y10"]},
        "y_ticks": y_ticks, "x_ticks": x_ticks[-12:],
        "points": [
            {"x": x(i), "y2": y(p["y2"]), "y10": y(p["y10"]), "d": p["d"], "v2": p["y2"],
             "v10": p["y10"]}
            for i, p in enumerate(points)
        ],
    }  # fmt: skip
