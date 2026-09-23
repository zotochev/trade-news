"""Entry point: `trade-news <command>`.

db-upgrade          apply migrations
sources             list registered collectors and whether they can run
collect <source>..  run collectors once
run                 run all enabled collectors on their schedules (blocking)
stats               collected / duplicates / errors per source
subscribers         Telegram subscribers
assets-seed         load assets.yaml + SEC company tickers into assets/asset_aliases
annotate            run the LLM annotation job once
review              print a random sample of annotations for manual checking
query ...           the spec's target queries (section 3)
cleanup             delete data older than the retention settings
admin               the admin web page alone (127.0.0.1:ADMIN_PORT); `run` starts it too
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import sqlalchemy as sa
import structlog
from dotenv import load_dotenv

from trade_news import delivery, llm, queries, retention
from trade_news.admin import app as admin_app
from trade_news.annotation import assets as asset_ref
from trade_news.annotation.run import annotate_pending
from trade_news.collectors import discover
from trade_news.config import Config, load_config
from trade_news.db import make_engine
from trade_news.db.schema import (
    annotation_dead_letters,
    annotations,
    asset_resolution_queue,
    assets,
    collector_runs,
    item_assets,
    item_relevance,
    items,
    llm_calls,
    subscribers,
)
from trade_news.http import RateLimiter
from trade_news.logs import setup_logging
from trade_news.pipeline import WRITE_LOCK, make_context, missing_secrets, run_source, utcnow
from trade_news.telegram import bot
from trade_news.telegram.api import make_api

log = structlog.get_logger()


def runnable_sources(cfg: Config, names: list[str] | None = None) -> list:
    registry = discover()
    unknown = [n for n in (names or cfg.sources) if n not in registry]
    for name in unknown:
        log.error("source_not_registered", source=name)
    specs = []
    for name in names or [n for n, s in cfg.sources.items() if s.enabled]:
        spec = registry.get(name)
        if spec is None:
            continue
        if name not in cfg.sources:
            log.error("source_not_configured", source=name)
            continue
        if missing := missing_secrets(spec):
            log.warning("source_skipped_missing_secrets", source=name, missing=missing)
            continue
        specs.append(spec)
    return specs


def cmd_db_upgrade(cfg: Config) -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    if cfg.database_url.startswith("sqlite:///"):
        Path(cfg.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent
    acfg = AlembicConfig()
    acfg.set_main_option("script_location", str(root / "migrations"))
    acfg.set_main_option("sqlalchemy.url", cfg.database_url)
    command.upgrade(acfg, "head")
    log.info("db_upgraded", url=cfg.database_url)


def cmd_sources(cfg: Config) -> None:
    for name, spec in sorted(discover().items()):
        scfg = cfg.sources.get(name)
        state = "not configured" if scfg is None else ("enabled" if scfg.enabled else "disabled")
        missing = missing_secrets(spec)
        print(f"{name:28} {state:15} {'missing env: ' + ', '.join(missing) if missing else ''}")


def cmd_collect(cfg: Config, names: list[str]) -> None:
    engine = make_engine(cfg.database_url)
    limiters: dict[str, RateLimiter] = {}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for spec in runnable_sources(cfg, names):
            run_source(engine, spec, cfg, make_context(spec, cfg, limiters, client))


def cmd_run(cfg: Config) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    engine = make_engine(cfg.database_url)
    limiters: dict[str, RateLimiter] = {}
    client = httpx.Client(timeout=30, follow_redirects=True)
    specs = runnable_sources(cfg)
    with engine.begin() as conn:  # runs left "running" by a killed process
        aborted = conn.execute(
            collector_runs.update()
            .where(collector_runs.c.status == "running")
            .values(status="aborted", finished_at=utcnow())
        ).rowcount
    if aborted:
        log.warning("stale_runs_marked_aborted", count=aborted)
    scheduler = BlockingScheduler(
        timezone="UTC",
        executors={"default": {"type": "threadpool", "max_workers": max(4, len(specs))}},
    )
    for spec in specs:
        ctx = make_context(spec, cfg, limiters, client)
        scheduler.add_job(
            run_source,
            "interval",
            args=(engine, spec, cfg, ctx),
            seconds=cfg.sources[spec.name].interval_seconds,
            next_run_time=utcnow(),
            id=spec.name,
            max_instances=1,  # a slow run is never overlapped by the next one
            coalesce=True,
            misfire_grace_time=60,
        )

    try:
        seed_assets(engine, client, sec=_no_equities(engine))
    except Exception:
        log.exception("assets_seed_failed")  # annotation still works, resolution is weaker
    llm_client = None
    if (missing := llm.missing_secret(cfg.llm)) is None:
        llm_client = llm.make_client(cfg.llm, engine, client, utcnow)
        scheduler.add_job(
            run_annotate,
            "interval",
            args=(engine, llm_client, cfg),
            seconds=cfg.llm.interval_seconds,
            next_run_time=utcnow() + timedelta(seconds=30),  # let collectors fill items first
            id="annotate",
            max_instances=1,
            coalesce=True,
        )
    else:
        log.warning("annotation_disabled", missing_env=missing)
    scheduler.add_job(
        run_cleanup,
        "interval",
        args=(engine, cfg),
        hours=cfg.retention.interval_hours,
        next_run_time=utcnow() + timedelta(minutes=5),
        id="cleanup",
        max_instances=1,
        coalesce=True,
    )

    source_list = [(s.name, s.description) for s in specs]
    bot_thread, bot_stop = start_bot(engine, client, source_list) or (None, None)
    if (tg_api := telegram_api(client)) is not None:
        scheduler.add_job(
            run_delivery_job,
            "interval",
            args=(engine, tg_api),
            seconds=60,
            next_run_time=utcnow() + timedelta(seconds=45),
            id="delivery",
            max_instances=1,
            coalesce=True,
        )
    admin_server = admin_app.start_in_thread(
        admin_app.create_app(
            admin_deps(engine, cfg, source_list, llm_client, scheduler, bot_thread)
        ),
        admin_port(),
    )

    def stop(signum, _frame):
        # systemctl stop sends SIGTERM: let running collectors finish their transaction
        log.info("scheduler_stopping", signal=signal.Signals(signum).name)
        if bot_stop is not None:
            bot_stop.set()
        admin_server.should_exit = True
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("scheduler_start", sources=[s.name for s in specs])
    try:
        scheduler.start()
    finally:
        client.close()
        log.info("scheduler_stopped")


def run_annotate(engine: sa.Engine, llm_client, cfg: Config) -> None:
    try:
        annotate_pending(engine, llm_client, cfg.llm, utcnow())
    except Exception:
        log.exception("annotate_job_failed")


def run_cleanup(engine: sa.Engine, cfg: Config) -> None:
    try:
        retention.cleanup(engine, cfg.retention, utcnow(), write_lock=WRITE_LOCK)
    except Exception:
        log.exception("cleanup_job_failed")


def _no_equities(engine: sa.Engine) -> bool:
    with engine.connect() as conn:
        count = sa.select(sa.func.count()).where(assets.c.asset_class == "equity")
        return not conn.execute(count).scalar()


def seed_assets(engine: sa.Engine, client: httpx.Client, sec: bool) -> None:
    """assets.yaml is always synced (cheap); SEC tickers (~10k) when asked or on first run."""
    with engine.begin() as conn:
        added, aliases = asset_ref.upsert_assets(conn, asset_ref.yaml_assets(), "assets.yaml")
    log.info("assets_seeded", source="assets.yaml", assets=added, aliases=aliases)
    if not sec:
        return
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        log.warning("assets_sec_skipped", reason="SEC_USER_AGENT is not set")
        return
    resp = client.get(asset_ref.SEC_TICKERS_URL, headers={"User-Agent": ua}, timeout=60)
    resp.raise_for_status()
    with engine.begin() as conn:
        added, aliases = asset_ref.upsert_assets(conn, asset_ref.sec_equities(resp.json()), "sec")
    log.info("assets_seeded", source="sec", assets=added, aliases=aliases)


def cmd_assets_seed(cfg: Config) -> None:
    engine = make_engine(cfg.database_url)
    with httpx.Client(follow_redirects=True) as client:
        seed_assets(engine, client, sec=True)


def cmd_annotate(cfg: Config, batches: int | None) -> None:
    if missing := llm.missing_secret(cfg.llm):
        raise SystemExit(f"{missing} is not set")
    engine = make_engine(cfg.database_url)
    if _no_equities(engine):
        log.warning("assets_empty", hint="run `trade-news assets-seed` first")
    llm_cfg = cfg.llm.model_copy(update={"max_batches_per_run": batches}) if batches else cfg.llm
    with httpx.Client(follow_redirects=True) as client:
        llm_client = llm.make_client(llm_cfg, engine, client, utcnow)
        annotate_pending(engine, llm_client, llm_cfg, utcnow())


def cmd_cleanup(cfg: Config) -> None:
    retention.cleanup(make_engine(cfg.database_url), cfg.retention, utcnow())


def cmd_review(cfg: Config, n: int) -> None:
    """Random sample of annotated items, for the manual check in the stage 3 acceptance."""
    engine = make_engine(cfg.database_url)
    sample = (
        sa.select(
            items.c.id,
            items.c.source,
            items.c.published_at,
            items.c.title,
            annotations.c.payload_json,
        )
        .join(annotations, annotations.c.item_id == items.c.id)
        .order_by(sa.func.random())
        .limit(n)
    )
    with engine.connect() as conn:
        for r in conn.execute(sample).all():
            links = conn.execute(
                sa.select(item_assets, assets.c.symbol)
                .outerjoin(assets, assets.c.id == item_assets.c.asset_id)
                .where(item_assets.c.item_id == r.id)
                .order_by(item_assets.c.importance.desc())
            ).all()
            rel = conn.execute(
                sa.select(item_relevance).where(item_relevance.c.item_id == r.id)
            ).first()
            p = r.payload_json
            print(f"#{r.id} [{r.source}] {r.published_at:%Y-%m-%d %H:%M} {r.title}")
            print(f"   summary: {p.get('summary')}  ({p.get('event_type')})")
            for a in links:
                target = a.symbol or (f"?{a.raw_symbol}" if a.raw_symbol else a.group_label or "*")
                primary = " primary" if a.is_primary else ""
                print(
                    f"   - {a.asset_class}/{a.scope} {target} {a.direction or '-'} "
                    f"imp={a.importance} conf={a.confidence}{primary}"
                )
            if rel:
                when = f"{rel.relevant_from:%Y-%m-%d %H:%M}" if rel.relevant_from else ""
                flag = " NEEDS REVIEW" if rel.needs_review else ""
                print(
                    f"   relevance: {rel.relevance_type} {when} ({rel.date_precision}, "
                    f"by {rel.resolved_by}) phrase={rel.raw_phrase!r}{flag}"
                )
            print()


def cmd_query(cfg: Config, args) -> None:
    now = utcnow()
    if args.name != "scheduled" and not args.target:
        raise SystemExit(f"query {args.name} needs a target (symbol or asset class)")
    match args.name:
        case "asset-news":
            q = queries.asset_news(args.target, now - timedelta(days=args.days), now)
        case "class-day":
            q = queries.class_on_day(args.target, _parse_day(args.date, now))
        case "upcoming":
            q = queries.asset_upcoming(args.target, now, args.days)
        case _:
            q = queries.scheduled_future(now)
    with make_engine(cfg.database_url).connect() as conn:
        rows = conn.execute(q.limit(args.limit)).all()
    for r in rows:
        when = f"{r.relevant_from:%Y-%m-%d %H:%M}" if r.relevant_from else "-"
        print(
            f"{r.published_at:%m-%d %H:%M}  {r.asset_class}/{r.scope} {r.symbol or ''} "
            f"imp={r.importance} {r.direction or '-'}  {r.relevance_type}@{when}  {r.title}"
        )
    print(f"{len(rows)} rows")


def _parse_day(value: str, now: datetime) -> date:
    named = {"today": now.date(), "tomorrow": now.date() + timedelta(days=1)}
    return named.get(value) or date.fromisoformat(value)


def root_chat_id() -> int | None:
    value = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return int(value) if value else None


def telegram_api(client: httpx.Client):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    return make_api(client, token) if token and root_chat_id() is not None else None


def run_delivery_job(engine: sa.Engine, api) -> None:
    try:
        delivery.run_delivery(engine, api, root_chat_id(), utcnow)
    except Exception:
        log.exception("delivery_job_failed")


def admin_port() -> int:
    return int(os.environ.get("ADMIN_PORT") or 10000)


def admin_deps(engine, cfg: Config, sources, llm_client, scheduler=None, bot_thread=None):
    """Wires the admin page's buttons to the running jobs."""

    def annotate_now() -> None:
        if scheduler is not None and scheduler.get_job("annotate"):
            scheduler.modify_job("annotate", next_run_time=utcnow())
        else:
            threading.Thread(
                target=run_annotate, args=(engine, llm_client, cfg), daemon=True
            ).start()

    def reannotate(item_id: int) -> None:
        def job():
            try:
                annotate_pending(engine, llm_client, cfg.llm, utcnow(), only_ids=[item_id])
            except Exception:
                log.exception("reannotate_failed", item_id=item_id)

        threading.Thread(target=job, daemon=True).start()

    return admin_app.AdminDeps(
        engine=engine,
        cfg=cfg,
        sources=sources,
        now=utcnow,
        annotate_now=annotate_now if llm_client is not None else None,
        reannotate=reannotate if llm_client is not None else None,
        bot_running=(bot_thread.is_alive if bot_thread is not None else None),
        delivery_configured=bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
        and root_chat_id() is not None,
    )


def cmd_admin(cfg: Config) -> None:
    """The admin page alone (no collectors, no bot): handy for local development."""
    import uvicorn

    engine = make_engine(cfg.database_url)
    registry = discover()
    sources = [
        (n, registry[n].description) for n, s in cfg.sources.items() if s.enabled and n in registry
    ]
    with httpx.Client(follow_redirects=True) as client:
        llm_client = (
            None
            if llm.missing_secret(cfg.llm)
            else llm.make_client(cfg.llm, engine, client, utcnow)
        )
        app = admin_app.create_app(admin_deps(engine, cfg, sources, llm_client))
        log.info("admin_started", url=f"http://127.0.0.1:{admin_port()}")
        uvicorn.run(app, host="127.0.0.1", port=admin_port(), log_level="warning")


def start_bot(engine: sa.Engine, client: httpx.Client, sources: list[tuple[str, str]]):
    """Starts the subscription bot thread if a token is configured. Returns (thread, stop event)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.warning("telegram_bot_disabled", reason="TELEGRAM_BOT_TOKEN is not set")
        return None
    return bot.start_in_thread(
        engine, make_api(client, token), root_chat_id=root_chat_id(), now=utcnow, sources=sources
    )


def cmd_subscribers(cfg: Config) -> None:
    engine = make_engine(cfg.database_url)
    s = subscribers.c
    with engine.connect() as conn:
        rows = conn.execute(sa.select(subscribers).order_by(s.is_active.desc(), s.subscribed_at))
        rows = rows.all()
    print(f"owner (TELEGRAM_CHAT_ID, always receives): {root_chat_id()}")
    for r in rows:
        state = "active" if r.is_active else f"off ({r.unsubscribe_reason})"
        print(f"{r.chat_id:>15}  {r.chat_type or '':10}  {state:15}  {r.title or ''}")
    print(f"active: {sum(r.is_active for r in rows)}, total: {len(rows)}")


def cmd_stats(cfg: Config, hours: float) -> None:
    engine = make_engine(cfg.database_url)
    since = utcnow() - timedelta(hours=hours)
    r = collector_runs.c
    q = (
        sa.select(
            r.source,
            sa.func.count().label("runs"),
            sa.func.sum(sa.case((r.status == "error", 1), else_=0)).label("errors"),
            sa.func.sum(r.fetched).label("fetched"),
            sa.func.sum(r.inserted).label("inserted"),
            sa.func.sum(r.seen_before).label("seen_before"),
            sa.func.sum(r.dedup_merged).label("dedup_merged"),
            sa.func.max(r.started_at).label("last_run"),
        )
        .where(r.started_at >= since)
        .group_by(r.source)
        .order_by(r.source)
    )
    with engine.connect() as conn:
        rows = conn.execute(q).all()
        total = conn.execute(sa.select(sa.func.count()).select_from(items)).scalar()
        groups = conn.execute(
            sa.select(sa.func.count(sa.distinct(items.c.dedup_group_id)))
        ).scalar()
    row_fmt = "{:28} {:>5} {:>4} {:>8} {:>6} {:>6} {:>7}  {}"
    print(f"last {hours:g}h")
    print(row_fmt.format("source", "runs", "err", "fetched", "new", "seen", "merged", "last_run"))
    for row in rows:
        counts = (row.fetched, row.inserted, row.seen_before, row.dedup_merged)
        last = f"{row.last_run:%Y-%m-%d %H:%M:%S}"
        print(row_fmt.format(row.source, row.runs, row.errors, *(c or 0 for c in counts), last))
    print(f"items total: {total}, unique stories (dedup groups): {groups}")
    print_llm_stats(engine, since)


def print_llm_stats(engine: sa.Engine, since: datetime) -> None:
    c = llm_calls.c
    with engine.connect() as conn:
        calls = conn.execute(
            sa.select(
                c.model,
                c.status,
                sa.func.count().label("n"),
                sa.func.coalesce(sa.func.sum(c.input_tokens), 0).label("tin"),
                sa.func.coalesce(sa.func.sum(c.output_tokens), 0).label("tout"),
                sa.func.coalesce(sa.func.sum(c.cost_estimate), 0).label("cost"),
            )
            .where(c.started_at >= since)
            .group_by(c.model, c.status)
        ).all()

        def count(table, *where):
            return conn.execute(
                sa.select(sa.func.count()).select_from(table).where(*where)
            ).scalar()

        annotated = count(annotations, annotations.c.created_at >= since)
        dead = count(annotation_dead_letters, annotation_dead_letters.c.created_at >= since)
        specific = count(
            item_assets.join(annotations, annotations.c.id == item_assets.c.annotation_id),
            item_assets.c.scope == "specific",
            annotations.c.created_at >= since,
        )
        unresolved = count(asset_resolution_queue, asset_resolution_queue.c.created_at >= since)
    print("LLM")
    for r in calls:
        print(
            f"  {r.model:24} {r.status:12} calls={r.n:<4} "
            f"tokens in={r.tin} out={r.tout}  ~${r.cost:.4f}"
        )
    share = f"{unresolved / specific:.1%}" if specific else "-"
    print(f"  annotated: {annotated}, dead letters: {dead}")
    print(f"  unresolved assets: {unresolved}/{specific} ({share})")


def main(argv: list[str] | None = None) -> None:
    load_dotenv(Path.cwd() / ".env")  # like config.yaml and data/: relative to the working dir
    p = argparse.ArgumentParser(prog="trade-news")
    p.add_argument("--config", default=None)
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    p.add_argument("--pretty", action="store_true", help="human-readable logs instead of JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("db-upgrade")
    sub.add_parser("sources")
    c = sub.add_parser("collect")
    c.add_argument("sources", nargs="*", help="default: all enabled")
    sub.add_parser("run")
    s = sub.add_parser("stats")
    s.add_argument("--hours", type=float, default=24)
    sub.add_parser("subscribers")
    sub.add_parser("assets-seed")
    a = sub.add_parser("annotate")
    a.add_argument("--batches", type=int, default=None, help="max batches (default: config)")
    r = sub.add_parser("review")
    r.add_argument("-n", type=int, default=20)
    sub.add_parser("cleanup")
    sub.add_parser("admin")
    q = sub.add_parser("query")
    q.add_argument("name", choices=["asset-news", "class-day", "upcoming", "scheduled"])
    q.add_argument("target", nargs="?", help="symbol (AAPL, EURUSD) or asset class (fx)")
    q.add_argument("--days", type=int, default=14)
    q.add_argument("--date", default="tomorrow", help="class-day: today | tomorrow | YYYY-MM-DD")
    q.add_argument("--limit", type=int, default=50)
    args = p.parse_args(argv)

    setup_logging(args.log_level, json=not args.pretty)
    cfg = load_config(args.config)
    match args.cmd:
        case "db-upgrade":
            cmd_db_upgrade(cfg)
        case "sources":
            cmd_sources(cfg)
        case "collect":
            cmd_collect(cfg, args.sources or None)
        case "run":
            cmd_run(cfg)
        case "stats":
            cmd_stats(cfg, args.hours)
        case "subscribers":
            cmd_subscribers(cfg)
        case "assets-seed":
            cmd_assets_seed(cfg)
        case "annotate":
            cmd_annotate(cfg, args.batches)
        case "review":
            cmd_review(cfg, args.n)
        case "cleanup":
            cmd_cleanup(cfg)
        case "admin":
            cmd_admin(cfg)
        case "query":
            cmd_query(cfg, args)


if __name__ == "__main__":
    main()
