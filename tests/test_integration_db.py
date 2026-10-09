"""End-to-end worker runs against a real MariaDB, with ACL and Bunny faked.

Needs a throwaway database (never QA/production): it TRUNCATEs tables.
    docker run -d --name wa-test-mariadb -e MARIADB_ROOT_PASSWORD=rootpw \
        -e MARIADB_DATABASE=vizipluskims -p 127.0.0.1:33065:3306 mariadb:10.5
    WA_TEST_DB=1 pytest tests/test_integration_db.py
"""
import json
import os
import uuid

import pytest
import requests

from app.acl import AclClient, TokenProvider
from app.bunny import BunnyClient
from app.db import connect
from app.schema import create_tables, create_triggers
from app.worker import Services, Worker

from tests.conftest import FakeResponse, FakeSession, accepted_response, token_response

pytestmark = pytest.mark.skipif(os.environ.get("WA_TEST_DB") != "1",
                                reason="set WA_TEST_DB=1 with a throwaway MariaDB on 127.0.0.1:33065")

AUTH = "https://auth.example.test/token"
OPTIN = "https://optin.example.test/bulk"
SEND = "https://api.example.test/messages"
UPLOAD = "https://storage.example.test/zone/*"

HOSPITAL_DDL = """
CREATE TABLE IF NOT EXISTS entry_exit_details (
  id bigint(20) NOT NULL AUTO_INCREMENT PRIMARY KEY,
  entry_details longtext, is_exit int(2) DEFAULT 0, entry_date datetime, exit_date datetime,
  is_regular int(2) DEFAULT 0, is_appoinment int(2) DEFAULT 0, slipnumber varchar(50),
  phone_number varchar(15)
) ENGINE=InnoDB DEFAULT CHARSET=latin1"""


@pytest.fixture
def db(settings):
    conn = connect(settings.db)
    with conn.cursor() as cur:
        cur.execute(HOSPITAL_DDL)
        for stmt in create_tables(settings.mapping) + create_triggers(settings.mapping):
            cur.execute(stmt.sql)
        for table in ("entry_exit_details", "wa_notification_queue", "wa_phone_optin"):
            cur.execute(f"TRUNCATE TABLE {table}")
    yield conn
    conn.close()


@pytest.fixture
def worker(settings, db):
    session = FakeSession()
    tokens = TokenProvider(settings.acl, session, 5)
    services = Services(settings=settings,
                        acl=AclClient(settings.acl, session, 5, tokens),
                        bunny=BunnyClient(settings.bunny, session, 5))
    w = Worker(settings, services)
    w.session = session
    yield w
    w._drop_connection()


def visit(db, phone="9876543210", exited=False, appointment=False):
    details = json.dumps({"personName": "Anand", "hostName": "KIMS", "companyName": "KIMS",
                          "departmentName": "GENERAL MEDICINE", "room_name": "ROOM 112",
                          "isAppointmentTime": "16-06-2026 12:30" if appointment else ""})
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO entry_exit_details (entry_details, entry_date, exit_date, slipnumber, "
            "phone_number, is_appoinment) VALUES (%s, NOW() - INTERVAL 30 MINUTE, "
            + ("NOW()" if exited else "NULL") + ", %s, %s, %s)",
            (details, f"slip{uuid.uuid4().hex[:8]}", phone, int(appointment)),
        )
        return cur.lastrowid


def mark_exit(db, source_id):
    with db.cursor() as cur:
        cur.execute("UPDATE entry_exit_details SET exit_date = NOW(), is_exit = 1 WHERE id = %s",
                    (source_id,))


def jobs(db):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM wa_notification_queue ORDER BY id")
        return cur.fetchall()


def make_due(db):
    with db.cursor() as cur:
        cur.execute("UPDATE wa_notification_queue SET next_attempt_at = UTC_TIMESTAMP(6) "
                    "WHERE status = 'PENDING'")


def test_entry_then_exit_accepted_and_opt_in_only_once(worker, db):
    s = worker.session
    s.add(AUTH, token_response())
    s.add(UPLOAD, FakeResponse(201), FakeResponse(201))
    s.add(OPTIN, FakeResponse(202, {}))
    s.add(SEND, accepted_response("resp-entry"), accepted_response("resp-exit"))

    source_id = visit(db)
    assert worker.run_cycle() == 1
    mark_exit(db, source_id)
    assert worker.run_cycle() == 1

    entry, exit_ = jobs(db)
    assert (entry["event_type"], entry["status"], entry["bsp_message_id"]) == ("ENTRY", "ACCEPTED", "resp-entry")
    assert (exit_["event_type"], exit_["status"], exit_["bsp_message_id"]) == ("EXIT", "ACCEPTED", "resp-exit")
    assert entry["pdf_url"].startswith("https://cdn.example.test/qa_entry/")
    assert exit_["pdf_url"].startswith("https://cdn.example.test/qa_exit/")
    assert uuid.UUID(entry["message_uuid"]).version == 7
    assert entry["message_uuid"] != entry["transaction_uuid"]
    assert entry["last_attempt_at"] is not None and entry["accepted_at"] is not None

    assert len(s.calls_to(OPTIN)) == 1          # second message skipped opt-in
    assert len(s.calls_to(AUTH)) == 1           # token reused
    sent = [c["json"] for c in s.calls_to(SEND)]
    assert sent[0]["to"] == "919876543210"
    assert sent[0]["metadata"] == {"messageId": entry["message_uuid"],
                                   "transactionId": entry["transaction_uuid"]}
    assert sent[0]["template"]["components"][0]["parameters"][0]["text"] == entry["pdf_url"]


def test_invalid_phone_fails_without_any_http_call(worker, db):
    visit(db, phone="12345")
    worker.run_cycle()
    (job,) = jobs(db)
    assert job["status"] == "FAILED"
    assert "invalid phone" in job["error_message"]
    assert worker.session.calls == []


def test_retry_reuses_message_ids_and_uploaded_pdf(worker, db):
    s = worker.session
    s.add(AUTH, token_response())
    s.add(UPLOAD, FakeResponse(201))
    s.add(OPTIN, FakeResponse(200, {}))
    s.add(SEND, FakeResponse(503, "busy"), accepted_response("resp-2"))

    visit(db)
    worker.run_cycle()
    (job,) = jobs(db)
    assert (job["status"], job["attempt_count"], job["last_http_status"]) == ("PENDING", 1, 503)
    assert job["next_attempt_at"] > job["updated_at"].replace(microsecond=0)  # backoff applied
    assert worker.run_cycle() == 0                                         # not due yet

    make_due(db)
    worker.run_cycle()
    (job2,) = jobs(db)
    assert (job2["status"], job2["attempt_count"]) == ("ACCEPTED", 2)
    first, second = (c["json"]["metadata"] for c in s.calls_to(SEND))
    assert first == second == {"messageId": job["message_uuid"], "transactionId": job["transaction_uuid"]}
    assert len([c for c in s.calls if c["method"] == "PUT"]) == 1          # PDF uploaded once
    assert len(s.calls_to(OPTIN)) == 1                                     # opted in once


def test_gives_up_after_max_attempts(worker, db, settings):
    s = worker.session
    s.add(UPLOAD, FakeResponse(500), FakeResponse(500), FakeResponse(500))
    visit(db)
    for _ in range(settings.worker.max_attempts):
        make_due(db)
        worker.run_cycle()
    (job,) = jobs(db)
    assert (job["status"], job["attempt_count"]) == ("FAILED", 3)
    assert "gave up after 3 attempts" in job["error_message"]


def test_client_error_fails_immediately(worker, db):
    s = worker.session
    s.add(AUTH, token_response())
    s.add(UPLOAD, FakeResponse(201))
    s.add(OPTIN, FakeResponse(200, {}))
    s.add(SEND, FakeResponse(400, {"error": "template mismatch"}))
    visit(db)
    worker.run_cycle()
    (job,) = jobs(db)
    assert (job["status"], job["attempt_count"], job["last_http_status"]) == ("FAILED", 1, 400)


def test_auth_failure_puts_job_back_without_using_an_attempt(worker, db):
    s = worker.session
    s.add(UPLOAD, FakeResponse(201))
    s.add(OPTIN, FakeResponse(200, {}))
    s.add(AUTH, FakeResponse(401, {"error": "invalid_grant"}))
    visit(db)
    visit(db, phone="9000000001")
    worker.run_cycle()
    first, second = jobs(db)
    assert (first["status"], first["attempt_count"]) == ("PENDING", 0)
    assert "authentication failed" in first["error_message"]
    assert first["pdf_url"] is not None                 # progress kept for the next try
    assert second["status"] == "PENDING" and second["attempt_count"] == 0  # paused, untouched
    assert len(s.calls_to(AUTH)) == 1                   # no hammering the auth server


def test_network_error_during_send_leaves_attempt_timestamp(worker, db):
    s = worker.session
    s.add(AUTH, token_response())
    s.add(UPLOAD, FakeResponse(201))
    s.add(OPTIN, FakeResponse(200, {}))
    s.add(SEND, requests.ConnectionError("reset"))
    visit(db)
    worker.run_cycle()
    (job,) = jobs(db)
    assert job["status"] == "PENDING"
    assert job["last_attempt_at"] is not None           # we know ACL may have received it


def test_stuck_jobs_are_reset_or_failed(worker, db, settings):
    visit(db)
    visit(db, phone="9000000001")
    with db.cursor() as cur:
        cur.execute("UPDATE wa_notification_queue SET status = 'PROCESSING', "
                    "processing_started_at = UTC_TIMESTAMP(6) - INTERVAL 30 MINUTE, "
                    "attempt_count = IF(phone = '9876543210', 1, %s)", (settings.worker.max_attempts,))
    reset, failed = worker._connect().reset_stuck(settings.worker.stuck_job_minutes,
                                                   settings.worker.max_attempts)
    assert (reset, failed) == (1, 1)
    statuses = {j["phone"]: j["status"] for j in jobs(db)}
    assert statuses == {"9876543210": "PENDING", "9000000001": "FAILED"}


def test_late_update_from_an_old_claim_changes_nothing(worker, db):
    visit(db)
    repo = worker._connect()
    job = repo.claim_next()
    with db.cursor() as cur:   # simulate: reset as stuck and re-claimed by someone else
        cur.execute("UPDATE wa_notification_queue SET claim_id = 'someone-else'")
    repo.mark_accepted(job, "late", 200)
    (row,) = jobs(db)
    assert row["status"] == "PROCESSING" and row["bsp_message_id"] is None
