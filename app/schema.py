"""Every database object this service owns, generated from table_mapping.yaml.

The same statements are used three ways:
  - `python -m app.setup` executes them (no DELIMITER needed through the driver)
  - `python -m app.render_sql` writes them to sql/*.sql for review / manual runs
  - verify_schema() checks them at worker startup
"""
from __future__ import annotations

from dataclasses import dataclass

import pymysql

from app.config import Mapping
from app.db import quote as q

QUEUE_COLUMNS = (
    "id", "source_record_id", "event_type", "phone", "status", "attempt_count",
    "next_attempt_at", "claim_id", "message_uuid", "transaction_uuid", "file_token",
    "pdf_url", "pdf_uploaded_at", "bsp_message_id", "last_http_status", "error_message",
    "created_at", "processing_started_at", "last_attempt_at", "accepted_at", "updated_at",
)
OPTIN_COLUMNS = ("phone", "opted_in_at", "http_status")


@dataclass(frozen=True)
class Statement:
    title: str
    sql: str
    is_routine: bool = False  # trigger body: needs DELIMITER when run from the mysql client


def create_tables(m: Mapping) -> list[Statement]:
    queue = m.queue
    return [
        Statement(f"Queue table {queue['table']}", f"""\
CREATE TABLE IF NOT EXISTS {q(queue['table'])} (
  `id`                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `source_record_id`      BIGINT NOT NULL COMMENT '{m.source['table']}.{m.source['primary_key']}',
  `event_type`            ENUM('ENTRY','EXIT') NOT NULL,
  `phone`                 VARCHAR(20) NULL COMMENT 'as captured; normalised by the worker',
  `status`                ENUM('PENDING','PROCESSING','ACCEPTED','FAILED') NOT NULL DEFAULT 'PENDING',
  `attempt_count`         INT UNSIGNED NOT NULL DEFAULT 0,
  `next_attempt_at`       DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
  `claim_id`              CHAR(36) NULL COMMENT 'set atomically when a worker claims the job',
  `message_uuid`          CHAR(36) NULL COMMENT 'ACL metadata.messageId, fixed on first attempt',
  `transaction_uuid`      CHAR(36) NULL COMMENT 'ACL metadata.transactionId, fixed on first attempt',
  `file_token`            CHAR(32) NULL COMMENT 'random Bunny file name, fixed on first attempt',
  `pdf_url`               VARCHAR(500) NULL COMMENT 'set after upload; retries reuse it',
  `pdf_uploaded_at`       DATETIME(6) NULL,
  `bsp_message_id`        VARCHAR(100) NULL COMMENT 'ACL responseId',
  `last_http_status`      SMALLINT NULL,
  `error_message`         VARCHAR(1000) NULL,
  `created_at`            DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
  `processing_started_at` DATETIME(6) NULL,
  `last_attempt_at`       DATETIME(6) NULL COMMENT 'written BEFORE calling ACL',
  `accepted_at`           DATETIME(6) NULL,
  `updated_at`            DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_source_event` (`source_record_id`, `event_type`),
  KEY `ix_claim` (`status`, `next_attempt_at`, `id`),
  KEY `ix_claim_id` (`claim_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='WhatsApp acknowledgement jobs (KIMS WhatsApp service)'"""),
        Statement(f"Opt-in table {queue['optin_table']}", f"""\
CREATE TABLE IF NOT EXISTS {q(queue['optin_table'])} (
  `phone`        VARCHAR(15) NOT NULL COMMENT 'normalised, e.g. 919876543210',
  `opted_in_at`  DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
  `http_status`  SMALLINT NULL COMMENT 'ACL opt-in response status',
  PRIMARY KEY (`phone`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Numbers already opted in with ACL (KIMS WhatsApp service)'"""),
    ]


def _enqueue(m: Mapping, event: str) -> str:
    s = m.source
    return (
        f"INSERT IGNORE INTO {q(m.queue['table'])}\n"
        f"      (`source_record_id`, `event_type`, `phone`, `next_attempt_at`, `created_at`, `updated_at`)\n"
        f"    VALUES (NEW.{q(s['primary_key'])}, '{event}', LEFT(TRIM(NEW.{q(s['phone'])}), 20),\n"
        f"            UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6));"
    )


def create_triggers(m: Mapping) -> list[Statement]:
    s, queue = m.source, m.queue
    phone_present = f"NULLIF(TRIM(NEW.{q(s['phone'])}), '') IS NOT NULL"
    # The CONTINUE handler swallows any error raised inside the body, so a
    # queue problem can never roll back the hospital's own insert/update.
    return [
        Statement(f"Trigger {queue['insert_trigger']} (ENTRY, and EXIT if inserted already exited)", f"""\
CREATE OR REPLACE TRIGGER {q(queue['insert_trigger'])}
AFTER INSERT ON {q(s['table'])}
FOR EACH ROW
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLEXCEPTION BEGIN END;
  IF {phone_present} THEN
    {_enqueue(m, 'ENTRY')}
    IF NEW.{q(s['exit_time'])} IS NOT NULL THEN
      {_enqueue(m, 'EXIT')}
    END IF;
  END IF;
END""", is_routine=True),
        Statement(f"Trigger {queue['update_trigger']} (EXIT when exit time goes NULL -> value)", f"""\
CREATE OR REPLACE TRIGGER {q(queue['update_trigger'])}
AFTER UPDATE ON {q(s['table'])}
FOR EACH ROW
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLEXCEPTION BEGIN END;
  IF OLD.{q(s['exit_time'])} IS NULL AND NEW.{q(s['exit_time'])} IS NOT NULL
     AND {phone_present} THEN
    {_enqueue(m, 'EXIT')}
  END IF;
END""", is_routine=True),
    ]


def drop_triggers(m: Mapping) -> list[Statement]:
    return [
        Statement(f"Drop trigger {name}", f"DROP TRIGGER IF EXISTS {q(name)}")
        for name in (m.queue["insert_trigger"], m.queue["update_trigger"])
    ]


def report_queries(m: Mapping) -> list[Statement]:
    t = q(m.queue["table"])
    masked = "CONCAT(LEFT(`phone`, 2), 'XXXXX', RIGHT(`phone`, 3))"
    return [
        Statement("Jobs by status", f"""\
SELECT `status`, `event_type`, COUNT(*) AS jobs
FROM {t}
GROUP BY `status`, `event_type`
ORDER BY `status`, `event_type`"""),
        Statement("Oldest PENDING job (UTC; minutes waiting)", f"""\
SELECT COUNT(*) AS pending_jobs,
       MIN(`created_at`) AS oldest_created_utc,
       TIMESTAMPDIFF(MINUTE, MIN(`created_at`), UTC_TIMESTAMP(6)) AS oldest_age_minutes
FROM {t}
WHERE `status` = 'PENDING'"""),
        Statement("FAILED jobs, newest first", f"""\
SELECT `id`, `source_record_id`, `event_type`, {masked} AS phone,
       `attempt_count`, `last_http_status`, LEFT(`error_message`, 200) AS error,
       `last_attempt_at`, `created_at`
FROM {t}
WHERE `status` = 'FAILED'
ORDER BY `id` DESC
LIMIT 100"""),
        Statement("Uncertain jobs: ACL was being called when the worker stopped", f"""\
SELECT `id`, `source_record_id`, `event_type`, {masked} AS phone,
       `attempt_count`, `processing_started_at`, `last_attempt_at`
FROM {t}
WHERE `status` = 'PROCESSING' AND `last_attempt_at` IS NOT NULL
ORDER BY `id`"""),
    ]


def service_user_statements(m: Mapping, database: str) -> list[Statement]:
    user = f"'{m.service_user}'@'{m.service_user_host}'"
    db = q(database)
    return [
        Statement("Create the restricted login (replace CHANGE_ME first)",
                  f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY 'CHANGE_ME'"),
        Statement("Read the hospital table",
                  f"GRANT SELECT ON {db}.{q(m.source['table'])} TO {user}"),
        Statement("Read/write the queue",
                  f"GRANT SELECT, INSERT, UPDATE ON {db}.{q(m.queue['table'])} TO {user}"),
        Statement("Read/write the opt-in table",
                  f"GRANT SELECT, INSERT, UPDATE ON {db}.{q(m.queue['optin_table'])} TO {user}"),
    ]


def verify_source_table(conn, m: Mapping) -> list[str]:
    """Check table_mapping.yaml against the hospital table only."""
    s = m.source
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (s["table"],),
        )
        existing = {r["COLUMN_NAME"] for r in cur.fetchall()}
    if not existing:
        return [f"table {s['table']} does not exist"]
    missing = sorted({v for k, v in s.items() if k != "table"} - existing)
    return [f"table {s['table']} is missing column(s): {', '.join(missing)}"] if missing else []


def verify_schema(conn, m: Mapping) -> list[str]:
    """Return a list of problems; empty means everything this service needs exists."""
    problems: list[str] = []
    s, queue = m.source, m.queue
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN (%s, %s, %s)",
            (s["table"], queue["table"], queue["optin_table"]),
        )
        columns: dict[str, set[str]] = {}
        for row in cur.fetchall():
            columns.setdefault(row["TABLE_NAME"], set()).add(row["COLUMN_NAME"])

        expected = {
            s["table"]: {v for k, v in s.items() if k != "table"},
            queue["table"]: set(QUEUE_COLUMNS),
            queue["optin_table"]: set(OPTIN_COLUMNS),
        }
        for table, cols in expected.items():
            if table not in columns:
                problems.append(f"table {table} does not exist")
                continue
            missing = sorted(cols - columns[table])
            if missing:
                problems.append(f"table {table} is missing column(s): {', '.join(missing)}")

        cur.execute(
            "SELECT TRIGGER_NAME, EVENT_MANIPULATION, ACTION_TIMING, EVENT_OBJECT_TABLE, DEFINER "
            "FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME IN (%s, %s)",
            (queue["insert_trigger"], queue["update_trigger"]),
        )
        found = {r["TRIGGER_NAME"]: r for r in cur.fetchall()}
        for name, event in ((queue["insert_trigger"], "INSERT"), (queue["update_trigger"], "UPDATE")):
            row = found.get(name)
            if row is None:
                problems.append(f"trigger {name} does not exist")
            elif (row["EVENT_OBJECT_TABLE"], row["EVENT_MANIPULATION"], row["ACTION_TIMING"]) != (
                s["table"], event, "AFTER"
            ):
                problems.append(
                    f"trigger {name} is not AFTER {event} ON {s['table']} "
                    f"(found {row['ACTION_TIMING']} {row['EVENT_MANIPULATION']} ON {row['EVENT_OBJECT_TABLE']})"
                )

        # A trigger whose owner login was deleted makes every hospital insert
        # fail with ERROR 1449. Detect it when we are allowed to read mysql.user.
        for definer in sorted({r["DEFINER"] for r in found.values()}):
            user, _, host = definer.rpartition("@")
            try:
                cur.execute("SELECT 1 FROM mysql.user WHERE User = %s AND Host = %s", (user, host))
                exists = cur.fetchone() is not None
            except pymysql.MySQLError:
                continue  # no permission to check; not a problem in itself
            if not exists:
                problems.append(
                    f"CRITICAL: trigger owner {definer} no longer exists, so inserts into "
                    f"{s['table']} are failing. Re-run setup with an existing login now."
                )
    return problems
