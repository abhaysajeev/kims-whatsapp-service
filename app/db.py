"""Database connection helper (MariaDB or MySQL).

One plain connection per user (no pool): the worker is single-threaded and
processes one job at a time. READ COMMITTED keeps the worker from holding gap
locks that could make the hospital's inserts (and so our trigger) wait.
Works with MariaDB 10.5 and MySQL 5.7.
"""
from __future__ import annotations

import pymysql
from pymysql.cursors import DictCursor

from app.config import DatabaseSettings


def connect(db: DatabaseSettings) -> pymysql.connections.Connection:
    conn = pymysql.connect(
        host=db.host,
        port=db.port,
        user=db.user,
        password=db.password.get(),
        database=db.name,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )
    _set_isolation(conn)
    return conn


def _set_isolation(conn) -> None:
    """READ COMMITTED, unless statement-based binary logging forbids it (MySQL error
    1665); then REPEATABLE READ, which only means slightly longer row locks."""
    with conn.cursor() as cur:
        cur.execute("SELECT @@log_bin AS log_bin, @@SESSION.binlog_format AS fmt")
        row = cur.fetchone()
        statement_binlog = bool(row["log_bin"]) and str(row["fmt"]).upper() == "STATEMENT"
        level = "REPEATABLE READ" if statement_binlog else "READ COMMITTED"
        cur.execute(f"SET SESSION TRANSACTION ISOLATION LEVEL {level}")


def quote(identifier: str) -> str:
    """Backtick-quote a name. Names are validated as plain identifiers in config."""
    return f"`{identifier}`"
