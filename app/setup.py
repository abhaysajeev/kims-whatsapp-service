"""One-time database setup, using the DB login from .env.

    python -m app.setup            create/refresh tables and triggers, then verify
    python -m app.setup --check    verify only, change nothing

Safe to run again: tables use CREATE TABLE IF NOT EXISTS (existing rows are
kept) and triggers are dropped and re-created. Works on MariaDB and MySQL 5.7.
"""
from __future__ import annotations

import argparse
import sys

import pymysql

from app.config import ConfigError, load_settings
from app.db import connect
from app.schema import create_tables, create_triggers, verify_schema, verify_source_table

# MariaDB/MySQL error codes that mean "this login lacks a privilege"
_PRIVILEGE_ERRORS = {1044, 1045, 1142, 1227, 1419}


def _print_problems(problems: list[str]) -> None:
    for p in problems:
        print(f"  - {p}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.setup", description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify only, change nothing")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    m, db = settings.mapping, settings.db
    print(f"Database {db.name} on {db.host}:{db.port} as {db.user}")
    try:
        conn = connect(db)
    except pymysql.MySQLError as exc:
        print(f"Cannot connect: {exc}", file=sys.stderr)
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS v, CURRENT_USER() AS u")
            info = cur.fetchone()
        print(f"Server {info['v']}, connected as {info['u']}")

        if not args.check:
            source_missing = verify_source_table(conn, m)
            if source_missing:
                print("table_mapping.yaml does not match the hospital table:", file=sys.stderr)
                _print_problems(source_missing)
                return 1

            for stmt in create_tables(m) + create_triggers(m):
                try:
                    with conn.cursor() as cur:
                        cur.execute(stmt.sql)
                    print(f"  ok  {stmt.title}")
                except pymysql.MySQLError as exc:
                    code = exc.args[0] if exc.args else None
                    print(f"  FAILED  {stmt.title}: {exc}", file=sys.stderr)
                    if code == 1419:
                        print(
                            "  Binary logging is on: creating triggers needs the SUPER privilege, "
                            "or the DBA must SET GLOBAL log_bin_trust_function_creators = 1.",
                            file=sys.stderr,
                        )
                    elif code in _PRIVILEGE_ERRORS:
                        print(
                            f"  The login {db.user} lacks a privilege needed here "
                            "(CREATE for tables, TRIGGER for triggers).",
                            file=sys.stderr,
                        )
                    return 1

        problems = verify_schema(conn, m)
        if problems:
            print("Schema check FAILED:", file=sys.stderr)
            _print_problems(problems)
            if args.check:
                print("Run: python -m app.setup", file=sys.stderr)
            return 1

        print("Schema check passed: queue table, opt-in table and both triggers are in place.")
        if not args.check:
            print(
                f"\nNOTE: the triggers run as {info['u']}. Never delete that login while the\n"
                "triggers exist, or every insert into the hospital table will fail.\n"
                "To remove the service, run sql/uninstall.sql (drops the triggers first)."
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
