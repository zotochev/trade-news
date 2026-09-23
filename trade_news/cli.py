"""Entry point: `trade-news <command>`.

db-upgrade          apply migrations
sources             list registered collectors and whether they can run
collect <source>..  run collectors once
run                 run all enabled collectors on their schedules (blocking)
stats               collected / duplicates / errors per source
"""

from __future__ import annotations

import argparse
import os
import signal
from datetime import timedelta
from pathlib import Path

import httpx
import sqlalchemy as sa
import structlog
from dotenv import load_dotenv

from trade_news.collectors import discover
from trade_news.config import Config, load_config
from trade_news.db import make_engine
from trade_news.db.schema import collector_runs, items, subscribers
from trade_news.http import RateLimiter
from trade_news.logs import setup_logging
from trade_news.pipeline import make_context, missing_secrets, run_source, utcnow
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

    bot_stop = start_bot(engine, client, [(s.name, s.description) for s in specs])

    def stop(signum, _frame):
        # systemctl stop sends SIGTERM: let running collectors finish their transaction
        log.info("scheduler_stopping", signal=signal.Signals(signum).name)
        if bot_stop is not None:
            bot_stop.set()
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("scheduler_start", sources=[s.name for s in specs])
    try:
        scheduler.start()
    finally:
        client.close()
        log.info("scheduler_stopped")


def root_chat_id() -> int | None:
    value = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return int(value) if value else None


def start_bot(engine: sa.Engine, client: httpx.Client, sources: list[tuple[str, str]]):
    """Starts the subscription bot thread if a token is configured. Returns its stop event."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.warning("telegram_bot_disabled", reason="TELEGRAM_BOT_TOKEN is not set")
        return None
    _, stop_event = bot.start_in_thread(
        engine, make_api(client, token), root_chat_id=root_chat_id(), now=utcnow, sources=sources
    )
    return stop_event


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


if __name__ == "__main__":
    main()
