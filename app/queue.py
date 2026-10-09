"""All reads and writes of the queue and opt-in tables.

Every change to a claimed job is guarded by `id AND claim_id`, so if a job
was reset as stuck and re-claimed elsewhere, a late update from the old
claim changes nothing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.config import Mapping
from app.db import quote as q

NOW = "UTC_TIMESTAMP(6)"


@dataclass
class Job:
    id: int
    source_record_id: int
    event_type: str
    phone: str | None
    status: str
    attempt_count: int
    claim_id: str | None
    message_uuid: str | None
    transaction_uuid: str | None
    file_token: str | None
    pdf_url: str | None
    last_attempt_at: datetime | None

    @classmethod
    def from_row(cls, row: dict) -> "Job":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


_JOB_COLUMNS = ", ".join(q(c) for c in Job.__dataclass_fields__)


class QueueRepo:
    def __init__(self, conn, m: Mapping):
        self.conn = conn
        self.t = q(m.queue["table"])
        self.optin = q(m.queue["optin_table"])

    def _exec(self, sql: str, params: tuple = ()) -> int:
        with self.conn.cursor() as cur:
            return cur.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    # --- claiming -----------------------------------------------------------

    def reset_stuck(self, stuck_minutes: int, max_attempts: int) -> tuple[int, int]:
        """PROCESSING longer than stuck_minutes: back to PENDING, or FAILED if out of attempts."""
        stale = f"`status` = 'PROCESSING' AND `processing_started_at` < {NOW} - INTERVAL %s MINUTE"
        failed = self._exec(
            f"UPDATE {self.t} SET `status` = 'FAILED', `claim_id` = NULL, `updated_at` = {NOW}, "
            f"`error_message` = CONCAT('stuck in PROCESSING with no attempts left; last error: ', "
            f"COALESCE(`error_message`, 'none')) "
            f"WHERE {stale} AND `attempt_count` >= %s",
            (stuck_minutes, max_attempts),
        )
        reset = self._exec(
            f"UPDATE {self.t} SET `status` = 'PENDING', `claim_id` = NULL, "
            f"`next_attempt_at` = {NOW}, `updated_at` = {NOW} "
            f"WHERE {stale} AND `attempt_count` < %s",
            (stuck_minutes, max_attempts),
        )
        return reset, failed

    def claim_next(self) -> Job | None:
        """Atomically take the next due job (MariaDB 10.5 has no SKIP LOCKED)."""
        claim_id = str(uuid.uuid4())
        claimed = self._exec(
            f"UPDATE {self.t} SET `status` = 'PROCESSING', `claim_id` = %s, "
            f"`attempt_count` = `attempt_count` + 1, `processing_started_at` = {NOW}, "
            f"`updated_at` = {NOW} "
            f"WHERE `status` = 'PENDING' AND `next_attempt_at` <= {NOW} "
            f"ORDER BY `next_attempt_at`, `id` LIMIT 1",
            (claim_id,),
        )
        if not claimed:
            return None
        row = self._one(f"SELECT {_JOB_COLUMNS} FROM {self.t} WHERE `claim_id` = %s", (claim_id,))
        return Job.from_row(row) if row else None

    def get(self, job_id: int) -> Job | None:
        row = self._one(f"SELECT {_JOB_COLUMNS} FROM {self.t} WHERE `id` = %s", (job_id,))
        return Job.from_row(row) if row else None

    # --- per-job progress ---------------------------------------------------

    def _claimed(self, job: Job, assignments: str, params: tuple = ()) -> int:
        return self._exec(
            f"UPDATE {self.t} SET {assignments}, `updated_at` = {NOW} "
            f"WHERE `id` = %s AND `claim_id` = %s",
            params + (job.id, job.claim_id),
        )

    def fix_identifiers(self, job: Job, message_uuid: str, transaction_uuid: str,
                        file_token: str) -> None:
        """Set the ACL ids and Bunny file name once; retries keep the first values."""
        self._claimed(
            job,
            "`message_uuid` = COALESCE(`message_uuid`, %s), "
            "`transaction_uuid` = COALESCE(`transaction_uuid`, %s), "
            "`file_token` = COALESCE(`file_token`, %s)",
            (message_uuid, transaction_uuid, file_token),
        )
        row = self._one(
            f"SELECT `message_uuid`, `transaction_uuid`, `file_token` FROM {self.t} WHERE `id` = %s",
            (job.id,),
        )
        job.message_uuid = row["message_uuid"]
        job.transaction_uuid = row["transaction_uuid"]
        job.file_token = row["file_token"]

    def save_pdf_url(self, job: Job, url: str) -> None:
        self._claimed(job, f"`pdf_url` = %s, `pdf_uploaded_at` = {NOW}", (url,))
        job.pdf_url = url

    def mark_attempt_started(self, job: Job) -> None:
        """Written BEFORE calling ACL send: a crash after this point is 'uncertain'."""
        self._claimed(job, f"`last_attempt_at` = {NOW}")

    # --- outcomes -----------------------------------------------------------

    def mark_accepted(self, job: Job, bsp_message_id: str | None, http_status: int) -> None:
        self._claimed(
            job,
            f"`status` = 'ACCEPTED', `claim_id` = NULL, `accepted_at` = {NOW}, "
            "`bsp_message_id` = %s, `last_http_status` = %s, `error_message` = NULL",
            (bsp_message_id, http_status),
        )

    def mark_failed(self, job: Job, error: str, http_status: int | None) -> None:
        self._claimed(
            job,
            "`status` = 'FAILED', `claim_id` = NULL, `error_message` = %s, `last_http_status` = %s",
            (error[:1000], http_status),
        )

    def schedule_retry(self, job: Job, delay_minutes: int, error: str,
                       http_status: int | None) -> None:
        self._claimed(
            job,
            f"`status` = 'PENDING', `claim_id` = NULL, "
            f"`next_attempt_at` = {NOW} + INTERVAL %s MINUTE, "
            "`error_message` = %s, `last_http_status` = %s",
            (delay_minutes, error[:1000], http_status),
        )

    def release(self, job: Job, delay_seconds: int, error: str) -> None:
        """Put back WITHOUT using an attempt (our own service problem, not the job's)."""
        self._claimed(
            job,
            f"`status` = 'PENDING', `claim_id` = NULL, "
            f"`attempt_count` = GREATEST(`attempt_count` - 1, 0), "
            f"`next_attempt_at` = {NOW} + INTERVAL %s SECOND, `error_message` = %s",
            (delay_seconds, error[:1000]),
        )

    # --- opt-in ---------------------------------------------------------------

    def is_opted_in(self, phone: str) -> bool:
        return self._one(f"SELECT 1 AS x FROM {self.optin} WHERE `phone` = %s", (phone,)) is not None

    def record_opt_in(self, phone: str, http_status: int) -> None:
        self._exec(
            f"INSERT IGNORE INTO {self.optin} (`phone`, `opted_in_at`, `http_status`) "
            f"VALUES (%s, {NOW}, %s)",
            (phone, http_status),
        )

    def counts(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT `status`, COUNT(*) AS n FROM {self.t} GROUP BY `status`")
            return {r["status"]: int(r["n"]) for r in cur.fetchall()}
