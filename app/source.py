"""Read one visit from the hospital table into the fields the slip needs."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from app.config import Mapping
from app.db import quote as q
from app.logging_setup import fields

log = logging.getLogger(__name__)

DATETIME_FORMAT = "%d-%m-%Y %H:%M:%S"   # matches the Android app's entryTime


@dataclass(frozen=True)
class SlipData:
    source_id: int
    event_type: str                 # ENTRY or EXIT
    slip_number: str
    phone: str                      # as stored in the hospital table
    entry_time: datetime | None
    exit_time: datetime | None
    is_appointment: bool
    is_regular: bool
    visitor_name: str
    whom_to_visit: str
    company_name: str
    department: str
    visit_purpose: str
    block: str
    floor: str
    room: str
    visitor_count: str
    appointment_time: str           # "dd-MM-yyyy HH:mm" from the app, may be empty

    @property
    def qr_prefix(self) -> str:
        """The app's scanner: 0 = general, 1 = appointment, 2 = regular. Appointment wins."""
        if self.is_appointment:
            return "1"
        if self.is_regular:
            return "2"
        return "0"

    @property
    def qr_text(self) -> str:
        return f"{self.qr_prefix} {self.slip_number}"

    @property
    def duration(self) -> str:
        if not (self.entry_time and self.exit_time) or self.exit_time < self.entry_time:
            return ""
        minutes = int((self.exit_time - self.entry_time).total_seconds() // 60)
        return f"{minutes // 60} h {minutes % 60:02d} m"

    def template_values(self, pdf_url: str) -> dict[str, str]:
        """Values available to whatsapp_templates.yaml parameter sources."""
        return {
            "pdf_url": pdf_url,
            "visitor_name": self.visitor_name,
            "slip_number": self.slip_number,
            "event_type": self.event_type,
            "entry_time": format_dt(self.entry_time),
            "exit_time": format_dt(self.exit_time),
        }


def format_dt(value: datetime | None) -> str:
    return value.strftime(DATETIME_FORMAT) if value else ""


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def load_slip(conn, m: Mapping, source_id: int, event_type: str) -> SlipData | None:
    s = m.source
    sql = (
        f"SELECT {q(s['primary_key'])} AS id, {q(s['phone'])} AS phone, "
        f"{q(s['entry_time'])} AS entry_time, {q(s['exit_time'])} AS exit_time, "
        f"{q(s['slip_number'])} AS slip_number, {q(s['is_appointment'])} AS is_appointment, "
        f"{q(s['is_regular'])} AS is_regular, {q(s['details_json'])} AS details "
        f"FROM {q(s['table'])} WHERE {q(s['primary_key'])} = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (source_id,))
        row = cur.fetchone()
    if row is None:
        return None

    details: dict = {}
    if row["details"]:
        try:
            parsed = json.loads(row["details"])
            if isinstance(parsed, dict):
                details = parsed
        except (TypeError, ValueError):
            log.warning("details JSON unreadable; printing table columns only",
                        extra=fields(source_id=source_id))

    k = m.details_json_keys
    return SlipData(
        source_id=int(row["id"]),
        event_type=event_type,
        slip_number=_text(row["slip_number"]),
        phone=_text(row["phone"]),
        entry_time=row["entry_time"],
        exit_time=row["exit_time"],
        is_appointment=bool(row["is_appointment"]),
        is_regular=bool(row["is_regular"]),
        **{name: _text(details.get(k[name])) for name in (
            "visitor_name", "whom_to_visit", "company_name", "department", "visit_purpose",
            "block", "floor", "room", "visitor_count", "appointment_time",
        )},
    )
