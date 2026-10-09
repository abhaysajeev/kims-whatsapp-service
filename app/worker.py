"""Job processing and the polling loop."""
from __future__ import annotations

import logging
import secrets
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymysql
import requests

from app import pdf_generator
from app.acl import AclClient, TokenProvider, build_payload
from app.bunny import BunnyClient
from app.config import Settings
from app.db import connect
from app.errors import PermanentError, RetryableError, ServiceError
from app.logging_setup import fields
from app.phone import InvalidPhone, mask, normalize
from app.queue import Job, QueueRepo
from app.schema import verify_schema
from app.source import load_slip
from app.uuid7 import uuid7

log = logging.getLogger("worker")

# Pause after consecutive ServiceErrors (ACL login / Bunny key problems)
SERVICE_PAUSE_SECONDS = (60, 120, 300, 600)
STARTUP_FAILURE_SLEEP = 30


def backoff_minutes(settings: Settings, attempt: int) -> int:
    """Delay before the next try after `attempt` failed tries (1 -> first value)."""
    delays = settings.worker.retry_backoff_minutes
    return delays[min(attempt, len(delays)) - 1]


@dataclass
class Services:
    settings: Settings
    acl: AclClient
    bunny: BunnyClient
    generate_pdf: object = pdf_generator.generate

    @classmethod
    def create(cls, settings: Settings) -> "Services":
        session = requests.Session()
        timeout = settings.worker.http_timeout_seconds
        tokens = TokenProvider(settings.acl, session, timeout)
        return cls(
            settings=settings,
            acl=AclClient(settings.acl, session, timeout, tokens),
            bunny=BunnyClient(settings.bunny, session, timeout),
        )


def process_job(job: Job, repo: QueueRepo, conn, svc: Services) -> None:
    """Run one claimed job to ACCEPTED, or raise a JobError subclass."""
    s = svc.settings

    try:
        phone = normalize(job.phone, s.worker.default_country_code)
    except InvalidPhone as exc:
        raise PermanentError(f"invalid phone: {exc}") from None

    slip = load_slip(conn, s.mapping, job.source_record_id, job.event_type)
    if slip is None:
        raise PermanentError(
            f"source record {job.source_record_id} not found in {s.mapping.source['table']}"
        )

    repo.fix_identifiers(job, str(uuid7()), str(uuid7()), secrets.token_hex(16))

    if not job.pdf_url:
        try:
            pdf = svc.generate_pdf(slip)
        except Exception as exc:  # a layout/data bug: retrying the same data won't help
            raise PermanentError(f"PDF generation failed: {type(exc).__name__}: {exc}") from None
        if s.worker.pdf_output_dir:
            s.worker.pdf_output_dir.mkdir(parents=True, exist_ok=True)
            (s.worker.pdf_output_dir / f"{job.id}_{job.event_type}.pdf").write_bytes(pdf)
        folder = svc.bunny.folder_for(job.event_type)
        url = svc.bunny.upload(pdf, folder, f"{job.file_token}.pdf")
        repo.save_pdf_url(job, url)
        log.info("pdf uploaded", extra=fields(job=job.id, event=job.event_type, folder=folder))

    if not repo.is_opted_in(phone):
        status = svc.acl.opt_in(phone)
        repo.record_opt_in(phone, status)
        log.info("phone opted in", extra=fields(job=job.id, phone=mask(phone), http=status))

    payload = build_payload(
        s.template, phone, slip.template_values(job.pdf_url),
        job.message_uuid, job.transaction_uuid,
    )
    repo.mark_attempt_started(job)
    result = svc.acl.send(payload)
    repo.mark_accepted(job, result.response_id, result.http_status)
    log.info("message accepted by ACL", extra=fields(
        job=job.id, event=job.event_type, phone=mask(phone), status="ACCEPTED",
        bsp_message_id=result.response_id))


@dataclass
class Worker:
    settings: Settings
    services: Services
    stop_requested: bool = False
    paused_until: float = 0.0
    service_failures: int = 0
    db_failures: int = 0
    conn: object = field(default=None, repr=False)

    # --- lifecycle ------------------------------------------------------------

    def request_stop(self, signum, _frame) -> None:
        log.info("stop requested; finishing the current job", extra=fields(signal=signum))
        self.stop_requested = True

    def _connect(self) -> QueueRepo:
        if self.conn is None:
            self.conn = connect(self.settings.db)
        return QueueRepo(self.conn, self.settings.mapping)

    def _drop_connection(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def _touch_heartbeat(self) -> None:
        try:
            Path(self.settings.worker.heartbeat_file).touch()
        except OSError:
            log.warning("cannot write heartbeat file",
                        extra=fields(path=self.settings.worker.heartbeat_file))

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop_requested and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    def check_schema(self) -> bool:
        repo = self._connect()
        problems = verify_schema(repo.conn, self.settings.mapping)
        for problem in problems:
            log.error("schema problem", extra=fields(problem=problem))
        if problems:
            log.error("database is not set up; run: python -m app.main setup")
        return not problems

    # --- one cycle --------------------------------------------------------------

    def run_cycle(self) -> int:
        """Reset stuck jobs, then drain due jobs. Returns how many were processed."""
        w = self.settings.worker
        repo = self._connect()
        reset, failed = repo.reset_stuck(w.stuck_job_minutes, w.max_attempts)
        if reset or failed:
            log.warning("stuck jobs recovered", extra=fields(reset_to_pending=reset, failed=failed))

        processed = 0
        if not w.send_enabled:
            return processed  # WHATSAPP_SEND_ENABLED=false: jobs stay PENDING
        while not self.stop_requested and time.monotonic() >= self.paused_until:
            job = repo.claim_next()
            if job is None:
                break
            self.handle(job, repo)
            processed += 1
        return processed

    def handle(self, job: Job, repo: QueueRepo) -> None:
        w = self.settings.worker
        ctx = dict(job=job.id, event=job.event_type, source_id=job.source_record_id,
                   attempt=job.attempt_count)
        try:
            process_job(job, repo, self.conn, self.services)
            self.service_failures = 0
        except ServiceError as exc:
            self.service_failures += 1
            pause = SERVICE_PAUSE_SECONDS[min(self.service_failures, len(SERVICE_PAUSE_SECONDS)) - 1]
            repo.release(job, pause, str(exc))
            self.paused_until = time.monotonic() + pause
            log.error("service problem; job put back without using an attempt, pausing",
                      extra=fields(**ctx, error=str(exc), pause_seconds=pause))
        except PermanentError as exc:
            repo.mark_failed(job, str(exc), exc.http_status)
            log.error("job failed permanently",
                      extra=fields(**ctx, status="FAILED", http=exc.http_status, error=str(exc)))
        except pymysql.MySQLError:
            raise  # database trouble: the loop reconnects; the job is recovered as stuck
        except Exception as exc:
            retryable = exc if isinstance(exc, RetryableError) else RetryableError(
                f"unexpected error: {type(exc).__name__}: {exc}")
            if not isinstance(exc, RetryableError):
                log.exception("unexpected error while processing job", extra=fields(**ctx))
            if job.attempt_count >= w.max_attempts:
                repo.mark_failed(job, f"gave up after {job.attempt_count} attempts: {retryable}",
                                 retryable.http_status)
                log.error("job failed after max attempts",
                          extra=fields(**ctx, status="FAILED", error=str(retryable)))
            else:
                delay = backoff_minutes(self.settings, job.attempt_count)
                repo.schedule_retry(job, delay, str(retryable), retryable.http_status)
                log.warning("job will be retried",
                            extra=fields(**ctx, status="PENDING", retry_in_minutes=delay,
                                         error=str(retryable)))

    # --- main loop ----------------------------------------------------------------

    def run(self, once: bool = False) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        w = self.settings.worker

        try:
            if not self.check_schema():
                self._sleep(STARTUP_FAILURE_SLEEP)  # avoid a tight Docker restart loop
                return 1
        except pymysql.MySQLError as exc:
            log.error("cannot reach the database at startup", extra=fields(error=str(exc)))
            self._sleep(STARTUP_FAILURE_SLEEP)
            return 1

        log.info("worker started", extra=fields(
            poll_seconds=w.poll_interval_seconds, max_attempts=w.max_attempts,
            template=self.settings.template.name,
            entry_folder=self.settings.bunny.entry_folder,
            exit_folder=self.settings.bunny.exit_folder,
            send_enabled=w.send_enabled))
        if not w.send_enabled:
            log.warning("WHATSAPP_SEND_ENABLED=false: no jobs will be processed; "
                        "new jobs wait as PENDING until it is set to true")

        while not self.stop_requested:
            try:
                processed = self.run_cycle()
                self.db_failures = 0
                self._touch_heartbeat()
                if processed:
                    log.info("cycle done", extra=fields(processed=processed))
            except pymysql.MySQLError as exc:
                self.db_failures += 1
                self._drop_connection()
                log.error("database error; will reconnect", extra=fields(
                    error=str(exc), consecutive=self.db_failures,
                    limit=w.max_consecutive_db_failures))
                if self.db_failures >= w.max_consecutive_db_failures:
                    log.critical("too many consecutive database failures; exiting so Docker restarts")
                    return 1
            if once:
                break
            self._sleep(w.poll_interval_seconds)

        self._drop_connection()
        log.info("worker stopped")
        return 0


def run_worker(settings: Settings, once: bool = False) -> int:
    return Worker(settings, Services.create(settings)).run(once=once)

