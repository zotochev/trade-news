"""Admin web page (FastAPI + server-rendered Jinja2).

Listens on 127.0.0.1:ADMIN_PORT only. Access from outside goes through nginx (HTTPS +
auth_basic, see deploy/HTTPS.md). Because browsers resend basic-auth credentials to any page
that submits a form to this origin, POST requests with a foreign Origin are rejected.
"""

from __future__ import annotations

import csv
import io
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import sqlalchemy as sa
import structlog
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from trade_news import delivery, views
from trade_news.admin import data
from trade_news.config import Config

log = structlog.get_logger()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
ASSET_CLASSES = ("equity", "fx", "crypto", "commodity", "index", "rates", "macro")


@dataclass
class AdminDeps:
    engine: sa.Engine
    cfg: Config
    sources: list[tuple[str, str]]  # (name, description) of collectors this process runs
    now: Callable[[], datetime]
    annotate_now: Callable[[], None] | None = None  # kick the annotation job
    reannotate: Callable[[int], None] | None = None  # annotate one item right away
    bot_running: Callable[[], bool] | None = None
    delivery_configured: bool = False  # bot token and owner chat id are set


# --- template helpers ---------------------------------------------------------------


NARROW_NBSP = chr(0x202F)  # thousands separator
BOM = chr(0xFEFF)  # lets Excel open the CSV as UTF-8


def _int(n) -> str:
    return f"{int(n or 0):,}".replace(",", NARROW_NBSP)


def _dt(value: datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    return value.strftime(fmt) if value else "—"


def _ago(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "ещё не запускался"
    minutes = int((now - value).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 48 * 60:
        return f"{minutes // 60} ч назад"
    return f"{minutes // 1440} дн назад"


TEMPLATES.env.filters.update(int=_int, dt=_dt)
TEMPLATES.env.globals.update(ASSET_CLASSES=ASSET_CLASSES)

DIRECTION = {
    "bullish": ("бычий", "▲", "bull"),
    "bearish": ("медвежий", "▼", "bear"),
    "neutral": ("нейтрально", "●", "neut"),
}
TEMPLATES.env.globals.update(DIRECTION=DIRECTION)


def create_app(deps: AdminDeps) -> FastAPI:
    app = FastAPI(title="trade-news admin", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            host = request.headers.get("host", "")
            if origin and urlsplit(origin).netloc != host:
                log.warning("admin_cross_origin_post_rejected", origin=origin, host=host)
                return Response("cross-origin request rejected", status_code=403)
        return await call_next(request)

    def render(request: Request, template: str, active: str, **ctx) -> HTMLResponse:
        with deps.engine.connect() as conn:
            queue_size = len(data.asset_queue(conn))
        now = deps.now()
        return TEMPLATES.TemplateResponse(
            request,
            template,
            {
                "active": active,
                "now": now,
                "queue_size": queue_size,
                "ago": lambda v: _ago(v, now),
                **ctx,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, hours: int = 24):
        hours = hours if hours in (24, 168, 720) else 24
        with deps.engine.connect() as conn:
            ov = data.overview(conn, deps.cfg, deps.sources, deps.now(), hours)
        return render(
            request, "overview.html", "overview", ov=ov, can_annotate=deps.annotate_now is not None
        )

    @app.post("/annotate-now")
    def annotate_now():
        if deps.annotate_now:
            deps.annotate_now()
        return RedirectResponse("/?annotating=1", status_code=303)

    def _filter(request: Request) -> data.NewsFilter:
        p = request.query_params
        try:
            min_imp = int(p.get("min_importance") or 1)
        except ValueError:
            min_imp = 1
        return data.NewsFilter(
            q=p.get("q", ""),
            asset_class=p.get("asset_class", "") if p.get("asset_class") in ASSET_CLASSES else "",
            min_importance=min(max(min_imp, 1), 5),
            direction=p.get("direction", "") if p.get("direction") in DIRECTION else "",
            source=p.get("source", ""),
            review_only=p.get("review") == "1",
        )

    @app.get("/news", response_class=HTMLResponse)
    def news(request: Request, id: int | None = None):
        f = _filter(request)
        with deps.engine.connect() as conn:
            rows = data.news(conn, f)
            selected = views.news_item(conn, id or (rows[0]["id"] if rows else 0))
        qs = {k: v for k, v in request.query_params.items() if k != "id"}
        return render(
            request,
            "news.html",
            "news",
            rows=rows,
            f=f,
            selected=selected,
            qs=urlencode(qs),
            sources=[name for name, _ in deps.sources],
            can_reannotate=deps.reannotate is not None,
        )

    @app.get("/news.csv")
    def news_csv(request: Request):
        with deps.engine.connect() as conn:
            rows = data.news(conn, _filter(request))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "id",
                "published_at_utc",
                "source",
                "title",
                "summary",
                "event_type",
                "assets",
                "relevance",
                "relevant_from",
                "url",
            ]
        )
        for r in rows:
            rel = r["relevance"] or {}
            w.writerow(
                [
                    r["id"],
                    _dt(r["published_at"], "%Y-%m-%d %H:%M"),
                    r["source"],
                    r["title"],
                    r["summary"],
                    r["event_type"],
                    "; ".join(
                        f"{link['label']}:{link['direction'] or '-'}:{link['importance']}"
                        for link in r["links"]
                    ),
                    rel.get("relevance_type", ""),
                    _dt(rel.get("relevant_from"), "%Y-%m-%d %H:%M"),
                    r["canonical_url"] or "",
                ]
            )
        return Response(
            BOM + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="trade-news.csv"'},
        )

    @app.get("/news/{item_id}/raw")
    def news_raw(item_id: int):
        with deps.engine.connect() as conn:
            item = views.news_item(conn, item_id)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(item["payload_json"] or {})

    @app.post("/news/{item_id}/reannotate")
    def news_reannotate(item_id: int):
        with deps.engine.begin() as conn:
            data.reset_annotation(conn, item_id)
        if deps.reannotate:
            deps.reannotate(item_id)
        return RedirectResponse(f"/news?id={item_id}", status_code=303)

    @app.get("/llm", response_class=HTMLResponse)
    def llm(request: Request, days: int = 7):
        days = days if days in (1, 7, 30) else 7
        with deps.engine.connect() as conn:
            u = data.llm_usage(conn, deps.cfg, deps.now(), days)
        return render(request, "llm.html", "llm", u=u, models=deps.cfg.llm.models)

    @app.get("/assets", response_class=HTMLResponse)
    def assets_page(request: Request, name: str = "", cls: str = "", q: str | None = None):
        with deps.engine.connect() as conn:
            queue = data.asset_queue(conn)
            current = next(
                (x for x in queue if x["name"] == name and x["asset_class"] == cls), None
            )
            current = current or (queue[0] if queue else None)
            search = q if q is not None else (current["name"] if current else "")
            found = data.search_assets(conn, search) if current else []
            counts = data.asset_counts(conn)
        return render(
            request,
            "assets.html",
            "assets",
            queue=queue,
            current=current,
            search=search,
            found=found,
            counts=counts,
        )

    @app.post("/assets/link")
    def assets_link(
        name: str = Form(...),
        cls: str = Form(...),
        asset_id: int = Form(...),
        save_alias: str = Form(""),
    ):
        with deps.engine.begin() as conn:
            n = data.link_queue(conn, name, cls, asset_id, save_alias == "1", deps.now())
        log.info("admin_asset_linked", name=name, asset_class=cls, asset_id=asset_id, links=n)
        return RedirectResponse("/assets", status_code=303)

    @app.post("/assets/create")
    def assets_create(
        name: str = Form(...),
        cls: str = Form(...),
        symbol: str = Form(...),
        asset_class: str = Form(...),
        title: str = Form(""),
    ):
        if asset_class not in ASSET_CLASSES or not symbol.strip():
            return Response("bad asset", status_code=400)
        with deps.engine.begin() as conn:
            asset_id = data.create_asset(conn, asset_class, symbol, title)
            data.link_queue(conn, name, cls, asset_id, save_alias=True, now=deps.now())
        log.info("admin_asset_created", symbol=symbol, asset_class=asset_class)
        return RedirectResponse("/assets", status_code=303)

    @app.post("/assets/ignore")
    def assets_ignore(name: str = Form(...), cls: str = Form(...)):
        with deps.engine.begin() as conn:
            data.ignore_queue(conn, name, cls, deps.now())
        return RedirectResponse("/assets", status_code=303)

    def subscribers_view(request: Request, rules=None, unsaved=False, error=None):
        with deps.engine.connect() as conn:
            page = data.subscribers_page(conn, deps.now())
            rules = rules or delivery.load_rules(conn)
            dpage = data.delivery_page(conn, rules, deps.now())
        bot = None if deps.bot_running is None else deps.bot_running()
        return render(
            request,
            "subscribers.html",
            "subscribers",
            page=page,
            bot=bot,
            rules=rules,
            d=dpage,
            unsaved=unsaved,
            error=error,
            EVENT_TYPES=delivery.EVENT_TYPES,
            can_deliver=deps.delivery_configured,
        )

    @app.get("/subscribers", response_class=HTMLResponse)
    def subscribers_page(request: Request):
        return subscribers_view(request)

    @app.post("/delivery/rules", response_class=HTMLResponse)
    async def delivery_rules(request: Request):
        form = await request.form()
        try:
            rules = delivery.DeliveryRules(
                enabled=form.get("enabled") == "1",
                min_importance=int(form.get("min_importance") or 3),
                require_direction=form.get("require_direction") == "1",
                event_types=[e for e in form.getlist("event_types") if e in delivery.EVENT_TYPES],
                asset_classes=[c for c in form.getlist("asset_classes") if c in ASSET_CLASSES],
                watchlist=[
                    s.strip().upper()
                    for s in str(form.get("watchlist") or "").replace(";", ",").split(",")
                    if s.strip()
                ],
                watchlist_min_importance=int(form.get("watchlist_min_importance") or 3),
                max_per_hour=int(form.get("max_per_hour") or 6),
                max_age_hours=float(form.get("max_age_hours") or 6),
            )
        except (ValueError, ValidationError) as exc:
            return subscribers_view(request, error=f"Правила не сохранены: {exc}")
        if form.get("action") != "save":
            return subscribers_view(request, rules=rules, unsaved=True)
        with deps.engine.begin() as conn:
            delivery.save_rules(conn, rules, deps.now())
        log.info("admin_delivery_rules_saved", **rules.model_dump())
        return RedirectResponse("/subscribers?saved=1", status_code=303)

    return app


# --- running inside the collector process -------------------------------------------


def start_in_thread(app: FastAPI, port: int):
    """uvicorn in a daemon thread on 127.0.0.1. Returns the server (set should_exit to stop)."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    threading.Thread(target=server.run, name="admin-web", daemon=True).start()
    log.info("admin_started", url=f"http://127.0.0.1:{port}")
    return server
