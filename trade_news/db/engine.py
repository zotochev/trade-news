from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite


def make_engine(url: str) -> sa.Engine:
    engine = sa.create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        sa.event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
    cur.execute("PRAGMA busy_timeout=10000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def _insert(conn: sa.Connection, table: sa.Table):
    dialect = {"sqlite": sqlite, "postgresql": postgresql}[conn.dialect.name]
    return dialect.insert(table)


def insert_ignore_many(
    conn: sa.Connection, table: sa.Table, rows: list[dict[str, Any]], *conflict_cols
) -> int:
    """Bulk INSERT ... ON CONFLICT DO NOTHING (executemany). Returns the number of new rows."""
    if not rows:
        return 0
    stmt = _insert(conn, table).on_conflict_do_nothing(index_elements=list(conflict_cols))
    before = conn.execute(sa.select(sa.func.count()).select_from(table)).scalar()
    for i in range(0, len(rows), 1000):
        conn.execute(stmt, rows[i : i + 1000])
    return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar() - before


def insert_ignore(conn: sa.Connection, table: sa.Table, rows: list[dict[str, Any]], *conflict_cols):
    """INSERT ... ON CONFLICT DO NOTHING for SQLite and PostgreSQL. Returns inserted ids."""
    if not rows:
        return []
    stmt = _insert(conn, table).on_conflict_do_nothing(index_elements=list(conflict_cols))
    ids = []
    # row-by-row so RETURNING tells us exactly which rows were new
    for row in rows:
        res = conn.execute(stmt.values(**row).returning(table.c.id)).first()
        ids.append(res.id if res else None)
    return ids
