"""MariaDB connection helper.

One plain connection per user (no pool): the worker is single-threaded and
processes one job at a time. READ COMMITTED keeps the worker from holding gap
locks that could make the hospital's inserts (and so our trigger) wait.
"""
from __future__ import annotations

import pymysql
from pymysql.cursors import DictCursor

from app.config import DatabaseSettings


def connect(db: DatabaseSettings) -> pymysql.connections.Connection:
    return pymysql.connect(
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
        init_command="SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
    )


def quote(identifier: str) -> str:
    """Backtick-quote a name. Names are validated as plain identifiers in config."""
    return f"`{identifier}`"
