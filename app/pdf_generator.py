"""Slip PDF generation (WeasyPrint + templates/slip.html).

Interface used by the worker:  generate(slip: SlipData) -> bytes

Layout follows the device-printed slip, with the KIMSHEALTH logo on top: header,
ENTRY/EXIT DETAILS, date/time, label : value rows, and on ENTRY a centred QR the
Android app can scan ("<prefix> <slip number>"). Change the look in
templates/slip.html; the logo is templates/assets/kims_logo.png.

Write sample PDFs for review (no database needed):
    python -m app.pdf_generator pdf_samples/
"""
from __future__ import annotations

import base64
import io
import logging
import sys
from datetime import datetime
from pathlib import Path

import qrcode
import qrcode.image.svg
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import PROJECT_ROOT
from app.source import SlipData

log = logging.getLogger(__name__)

HEADER = "KIMSHEALTH TRIVANDRUM"
QR_CAPTION_LINES = ("This QR-code is only for access", "Entry details")  # break as on the printed slip
QR_CAPTION = " ".join(QR_CAPTION_LINES)
TEMPLATE_DIR = PROJECT_ROOT / "templates"
LOGO_URL = "assets/kims_logo.png"          # relative to TEMPLATE_DIR
LOGO_PATH = TEMPLATE_DIR / LOGO_URL
MAX_VALUE_CHARS = 80              # bounds wrapping from unexpectedly long JSON values
MEASURE_PAGE_MM = 600             # first pass: tall enough for any slip
FALLBACK_PAGE_MM = {"ENTRY": 215, "EXIT": 175}
PAGE_MARGIN_MM = 5
BOTTOM_PAD_MM = 1

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _value(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS - 1] + "…"


def _split_appointment(raw: str) -> tuple[str, str]:
    """'16-06-2026 12:30' -> ('16-06-2026', '12:30'); anything else -> (raw, '')."""
    parts = (raw or "").split()
    if len(parts) == 2:
        try:
            datetime.strptime(parts[0], "%d-%m-%Y")
            return parts[0], parts[1]
        except ValueError:
            pass
    return (raw or "").strip(), ""


def _stamp(date_label: str, time_label: str, value: datetime | None) -> dict:
    return {
        "date_label": date_label,
        "time_label": time_label,
        "date": value.strftime("%d-%m-%Y") if value else "",
        "time": value.strftime("%H:%M:%S") if value else "",
    }


def qr_data_uri(text: str) -> str:
    img = qrcode.make(
        text,
        image_factory=qrcode.image.svg.SvgPathImage,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=4,
    )
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


_logo_warned = False


def _logo() -> str:
    """Logo URL for the template, or "" (slip renders without it) if the file is missing."""
    global _logo_warned
    if LOGO_PATH.is_file():
        return LOGO_URL
    if not _logo_warned:
        log.warning("logo file missing; slips will render without it",
                    extra={"fields": {"path": str(LOGO_PATH)}})
        _logo_warned = True
    return ""


def slip_context(slip: SlipData) -> dict:
    """Everything the template prints, in order. Pure function (unit-tested)."""
    is_entry = slip.event_type == "ENTRY"

    if is_entry:
        stamps = [_stamp("DATE", "TIME", slip.entry_time)]
    else:
        stamps = [_stamp("ENTRY", "TIME", slip.entry_time),
                  _stamp("EXIT ", "TIME", slip.exit_time)]

    rows: list[tuple[str, str]] = [
        ("VISITOR NAME", slip.visitor_name),
        ("VISIT PURPOSE", slip.visit_purpose),     # printed even when blank, like the photo
        ("PHONE", slip.phone),
        ("WHOM TO VISIT", slip.whom_to_visit),
        ("COMPANY NAME", slip.company_name),
        ("DEPARTMENT", slip.department),
    ]
    rows += [(label, value) for label, value in
             (("BLOCK", slip.block), ("FLOOR", slip.floor), ("ROOM", slip.room)) if value]
    if slip.is_appointment and slip.appointment_time:
        date, time = _split_appointment(slip.appointment_time)
        rows.append(("Appointment Date", date))
        if time:
            rows.append(("Appointment Time", time))
    if not is_entry and slip.duration:
        rows.append(("DURATION", slip.duration))

    return {
        "logo": _logo(),
        "header": HEADER,
        "title": "ENTRY DETAILS" if is_entry else "EXIT DETAILS",
        "stamps": stamps,
        "rows": [(label, _value(value)) for label, value in rows],
        "show_qr": is_entry and bool(slip.slip_number),
        "qr_caption": QR_CAPTION,
        "qr_caption_lines": QR_CAPTION_LINES,
        "qr_text": slip.qr_text,
        "qr_data_uri": qr_data_uri(slip.qr_text) if is_entry and slip.slip_number else "",
    }


def _render(context: dict, page_height_mm: float):
    from weasyprint import HTML  # imported lazily: heavy, and only needed here

    html = _env.get_template("slip.html").render(page_height_mm=round(page_height_mm, 1), **context)
    return HTML(string=html, base_url=str(TEMPLATE_DIR)).render()


def _content_height_mm(document) -> float:
    """Bottom of the laid-out <html> box on the first page, in mm.

    Uses WeasyPrint's layout tree (not a public API); callers fall back to a
    fixed page height if it ever changes.
    """
    html_box = document.pages[0]._page_box.children[0]
    bottom_px = html_box.position_y + html_box.margin_height()
    return bottom_px * 25.4 / 96


def generate(slip: SlipData) -> bytes:
    context = slip_context(slip)
    try:
        measured = _render(context, MEASURE_PAGE_MM)
        height = _content_height_mm(measured) + PAGE_MARGIN_MM + BOTTOM_PAD_MM
        document = _render(context, height)
        if len(document.pages) != 1:
            raise ValueError(f"fitted slip spans {len(document.pages)} pages")
    except (AttributeError, IndexError, ValueError) as exc:
        log.warning("could not fit page height; using fixed height", extra={"fields": {"error": str(exc)}})
        document = _render(context, FALLBACK_PAGE_MM[slip.event_type])
    return document.write_pdf()


# --- sample PDFs for visual review ------------------------------------------------

def sample_slips() -> dict[str, SlipData]:
    base = dict(
        source_id=142, slip_number="ffa9a2e4e98600fa20261007094834", phone="9898653298",
        entry_time=datetime(2026, 10, 7, 9, 49, 50), exit_time=None,
        is_appointment=False, is_regular=False, visitor_name="Anand", whom_to_visit="ROOM1",
        company_name="KIMS", department="GENERAL MEDICINE", visit_purpose="Patient Visit",
        block="", floor="", room="", visitor_count="1", appointment_time="",
    )
    return {
        "entry": SlipData(event_type="ENTRY", **base),
        "entry_appointment_full": SlipData(event_type="ENTRY", **{
            **base, "is_appointment": True, "appointment_time": "11-06-2026 12:30",
            "visitor_name": "Image test appointment with a much longer visitor name",
            "visit_purpose": "", "block": "BLOCK 99", "floor": "FLOOR 99", "room": "ROOM1",
        }),
        "exit": SlipData(event_type="EXIT", **{
            **base, "exit_time": datetime(2026, 10, 7, 11, 15, 5),
            "block": "BLOCK 123", "floor": "Floor 1", "room": "ROOM 112",
        }),
    }


def main(argv: list[str]) -> int:
    out = Path(argv[0] if argv else "pdf_samples")
    out.mkdir(parents=True, exist_ok=True)
    for name, slip in sample_slips().items():
        path = out / f"{name}.pdf"
        path.write_bytes(generate(slip))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
