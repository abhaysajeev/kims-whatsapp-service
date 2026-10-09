"""Command line entry point.

    python -m app.main                       run the worker (Docker CMD)
    python -m app.main --once                run one cycle and exit
    python -m app.main setup [--check]       create / verify database objects
    python -m app.main --dry-run --job 12
    python -m app.main --dry-run --source-id 142 --event ENTRY [--upload]

--dry-run never writes to the database and never calls ACL (no token, no
opt-in, no send). It builds the PDF and the exact send payload and prints them.
With --upload it also uploads the PDF to the configured Bunny folder so the
link can be opened.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from pathlib import Path

from app import setup as setup_cmd
from app.config import ConfigError, Settings, load_settings
from app.db import connect
from app.logging_setup import setup_logging


def _write_dry_run_pdf(settings: Settings, pdf: bytes, name: str) -> Path:
    """PDF_OUTPUT_DIR if set, else ./pdf_output, else /tmp (read-only container)."""
    candidates = [settings.worker.pdf_output_dir] if settings.worker.pdf_output_dir else []
    candidates += [Path("pdf_output"), Path(tempfile.gettempdir()) / "pdf_output"]
    for out_dir in candidates:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / name
            path.write_bytes(pdf)
            return path
        except OSError:
            continue
    raise OSError("no writable directory for the dry-run PDF")


def dry_run(settings: Settings, job_id: int | None, source_id: int | None,
            event: str | None, upload: bool) -> int:
    import requests

    from app import pdf_generator
    from app.acl import build_payload
    from app.bunny import BunnyClient
    from app.phone import InvalidPhone, normalize
    from app.queue import QueueRepo
    from app.source import load_slip
    from app.uuid7 import uuid7

    conn = connect(settings.db)
    try:
        message_uuid = transaction_uuid = file_token = None
        if job_id is not None:
            job = QueueRepo(conn, settings.mapping).get(job_id)
            if job is None:
                print(f"queue job {job_id} not found", file=sys.stderr)
                return 1
            source_id, event = job.source_record_id, job.event_type
            raw_phone = job.phone
            message_uuid, transaction_uuid, file_token = (
                job.message_uuid, job.transaction_uuid, job.file_token)
            print(f"job {job.id}: status={job.status} attempts={job.attempt_count}")

        slip = load_slip(conn, settings.mapping, source_id, event)
        if slip is None:
            print(f"source record {source_id} not found", file=sys.stderr)
            return 1
        if job_id is None:
            raw_phone = slip.phone
    finally:
        conn.close()

    try:
        phone = normalize(raw_phone, settings.worker.default_country_code)
        print(f"phone: OK ({phone})")
    except InvalidPhone as exc:
        print(f"phone: INVALID ({exc}); a real run would mark this job FAILED")
        return 1

    message_uuid = message_uuid or str(uuid7())
    transaction_uuid = transaction_uuid or str(uuid7())
    file_token = file_token or f"dryrun-{secrets.token_hex(8)}"

    pdf = pdf_generator.generate(slip)
    local = _write_dry_run_pdf(settings, pdf, f"dryrun_{source_id}_{event}.pdf")
    print(f"pdf: {len(pdf)} bytes written to {local}")

    bunny = BunnyClient(settings.bunny, requests.Session(), settings.worker.http_timeout_seconds)
    folder = bunny.folder_for(event)
    if upload:
        url = bunny.upload(pdf, folder, f"{file_token}.pdf")
        print(f"pdf uploaded: {url}")
    else:
        url = bunny.public_url(folder, f"{file_token}.pdf")
        print(f"pdf NOT uploaded (add --upload); link would be: {url}")

    payload = build_payload(settings.template, phone, slip.template_values(url),
                            message_uuid, transaction_uuid)
    print(f"\nPOST {settings.acl.send_url}")
    print("Authorization: Bearer ****")
    print("Content-Type: application/json")
    print(json.dumps(payload, indent=2))
    print("\nDRY RUN: no token requested, no opt-in, nothing sent, database unchanged.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["setup"]:
        return setup_cmd.main(argv[1:])

    parser = argparse.ArgumentParser(prog="python -m app.main", description="KIMS WhatsApp worker")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="build PDF and payload only")
    parser.add_argument("--job", type=int, help="dry run: queue job id")
    parser.add_argument("--source-id", type=int, help="dry run: hospital record id")
    parser.add_argument("--event", choices=("ENTRY", "EXIT"), help="dry run with --source-id")
    parser.add_argument("--upload", action="store_true", help="dry run: upload the PDF to Bunny")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.dry_run:
        if (args.job is None) == (args.source_id is None):
            parser.error("--dry-run needs either --job or --source-id")
        if args.source_id is not None and not args.event:
            parser.error("--source-id needs --event ENTRY or EXIT")
        return dry_run(settings, args.job, args.source_id, args.event, args.upload)

    setup_logging(settings.worker.log_level, settings.worker.log_format)
    from app.worker import run_worker
    return run_worker(settings, once=args.once)


if __name__ == "__main__":
    sys.exit(main())
