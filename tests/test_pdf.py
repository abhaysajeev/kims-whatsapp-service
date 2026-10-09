"""The real slip PDF: layout rules, one page, and a QR the Android app can scan.

The QR round-trip renders the actual PDF to an image and decodes it with ZXing
(zxing-cpp, the same decoder family the Android app uses).
"""
from datetime import datetime

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
import zxingcpp

from app import pdf_generator
from app.pdf_generator import QR_CAPTION, generate, slip_context

from tests.test_slip import slip

LONG_NAME = "Image test appointment with a much longer visitor name " * 3


def render(pdf: bytes):
    doc = pdfium.PdfDocument(pdf)
    page = doc[0]
    return doc, page.render(scale=3).to_pil(), page.get_textpage().get_text_range()


def decoded_qr(pdf: bytes) -> list[str]:
    _, image, _ = render(pdf)
    return [c.text for c in zxingcpp.read_barcodes(image)]


@pytest.mark.parametrize("overrides, expected_prefix", [
    ({}, "0"),
    ({"is_appointment": True, "appointment_time": "11-06-2026 12:30"}, "1"),
    ({"is_regular": True}, "2"),
    ({"is_appointment": True, "is_regular": True}, "1"),
])
def test_entry_qr_scans_to_the_app_format(overrides, expected_prefix):
    s = slip(**overrides)
    assert decoded_qr(generate(s)) == [f"{expected_prefix} {s.slip_number}"]


def test_exit_slip_has_no_qr_and_shows_duration():
    pdf = generate(slip(event_type="EXIT", exit_time=datetime(2026, 10, 7, 11, 15, 5)))
    assert decoded_qr(pdf) == []
    _, _, text = render(pdf)
    assert "EXIT DETAILS" in text and "DURATION" in text and "1 h 25 m" in text
    assert "QR-code" not in text


def test_worst_case_slip_is_exactly_one_page_with_readable_text():
    s = slip(visitor_name=LONG_NAME, is_appointment=True, appointment_time="11-06-2026 12:30",
             block="BLOCK 99", floor="FLOOR 99", room="ROOM1", visit_purpose="Patient Visit")
    doc, _, text = render(generate(s))
    assert len(doc) == 1
    for expected in ("KIMSHEALTH TRIVANDRUM", "ENTRY DETAILS", "9876543210", "Appointment Date",
                     "11-06-2026", "12:30", "This QR-code is only for access"):
        assert expected in text


def test_page_is_80mm_wide():
    doc, _, _ = render(generate(slip()))
    width_pt, _ = doc[0].get_size()
    assert round(width_pt * 25.4 / 72) == 80


def test_html_in_data_is_printed_literally():
    _, _, text = render(generate(slip(visitor_name="<b>Tom & Jerry</b>")))
    assert "<b>Tom & Jerry</b>" in text


def test_fixed_height_fallback_still_produces_a_pdf(monkeypatch):
    def broken(_document):
        raise AttributeError("layout API changed")
    monkeypatch.setattr(pdf_generator, "_content_height_mm", broken)
    s = slip()
    assert decoded_qr(generate(s)) == [s.qr_text]
    worst = slip(visitor_name=LONG_NAME, is_appointment=True, appointment_time="11-06-2026 12:30",
                 block="BLOCK 99", floor="FLOOR 99", room="ROOM1", visit_purpose="Patient Visit")
    doc, _, _ = render(generate(worst))
    assert len(doc) == 1
    exit_worst = slip(event_type="EXIT", exit_time=datetime(2026, 10, 7, 11, 0), visitor_name=LONG_NAME,
                      block="BLOCK 99", floor="FLOOR 99", room="ROOM1", visit_purpose="Patient Visit")
    doc, _, _ = render(generate(exit_worst))
    assert len(doc) == 1


def image_count(pdf: bytes) -> int:
    doc = pdfium.PdfDocument(pdf)
    return sum(1 for _ in doc[0].get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=5))


@pytest.mark.parametrize("event, overrides", [
    ("ENTRY", {}),
    ("EXIT", {"exit_time": datetime(2026, 10, 7, 11, 0)}),
])
def test_logo_is_embedded_on_every_slip(event, overrides):
    assert slip_context(slip(event_type=event, **overrides))["logo"] == pdf_generator.LOGO_URL
    assert image_count(generate(slip(event_type=event, **overrides))) >= 1


def test_missing_logo_file_still_renders_the_slip(monkeypatch, tmp_path):
    monkeypatch.setattr(pdf_generator, "LOGO_PATH", tmp_path / "missing.png")
    s = slip()
    assert slip_context(s)["logo"] == ""
    pdf = generate(s)
    assert image_count(pdf) == 0          # the QR is vector paths, so no image at all
    assert decoded_qr(pdf) == [s.qr_text]
    _, _, text = render(pdf)
    assert "KIMSHEALTH TRIVANDRUM" in text


# --- slip_context: what gets printed ------------------------------------------

def labels(ctx):
    return [label for label, _ in ctx["rows"]]


def test_entry_rows_like_the_printed_slip():
    ctx = slip_context(slip())
    assert ctx["title"] == "ENTRY DETAILS"
    assert labels(ctx) == ["VISITOR NAME", "VISIT PURPOSE", "PHONE", "WHOM TO VISIT",
                           "COMPANY NAME", "DEPARTMENT"]
    assert dict(ctx["rows"])["PHONE"] == "9876543210"          # full number
    assert dict(ctx["rows"])["VISIT PURPOSE"] == ""             # printed even if blank
    assert ctx["stamps"] == [{"date_label": "DATE", "time_label": "TIME",
                              "date": "07-10-2026", "time": "09:49:50"}]
    assert ctx["show_qr"] and ctx["qr_caption"] == "This QR-code is only for access Entry details"
    assert QR_CAPTION == ctx["qr_caption"]


def test_optional_rows_only_when_present():
    ctx = slip_context(slip(block="BLOCK 99", room="ROOM1"))
    assert "BLOCK" in labels(ctx) and "ROOM" in labels(ctx) and "FLOOR" not in labels(ctx)


def test_appointment_rows_only_for_appointment_visits():
    assert "Appointment Date" not in labels(slip_context(slip(appointment_time="11-06-2026 12:30")))
    ctx = slip_context(slip(is_appointment=True, appointment_time="11-06-2026 12:30"))
    rows = dict(ctx["rows"])
    assert (rows["Appointment Date"], rows["Appointment Time"]) == ("11-06-2026", "12:30")
    assert "Appointment Date" not in labels(slip_context(slip(is_appointment=True, appointment_time="")))


def test_unparseable_appointment_is_shown_whole():
    rows = dict(slip_context(slip(is_appointment=True, appointment_time="tomorrow noon"))["rows"])
    assert rows["Appointment Date"] == "tomorrow noon" and "Appointment Time" not in rows


def test_exit_context():
    ctx = slip_context(slip(event_type="EXIT", exit_time=datetime(2026, 10, 7, 10, 0, 0)))
    assert ctx["title"] == "EXIT DETAILS"
    assert not ctx["show_qr"] and ctx["qr_data_uri"] == ""
    assert [s["date_label"].strip() for s in ctx["stamps"]] == ["ENTRY", "EXIT"]
    assert dict(ctx["rows"])["DURATION"] == "0 h 10 m"


def test_entry_without_slip_number_has_no_qr():
    assert not slip_context(slip(slip_number=""))["show_qr"]


def test_very_long_values_are_bounded():
    value = dict(slip_context(slip(visitor_name="x" * 500))["rows"])["VISITOR NAME"]
    assert len(value) == pdf_generator.MAX_VALUE_CHARS
