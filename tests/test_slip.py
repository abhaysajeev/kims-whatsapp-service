from datetime import datetime

from app.source import SlipData
from app.worker import backoff_minutes


def slip(**overrides) -> SlipData:
    base = dict(
        source_id=142, event_type="ENTRY", slip_number="ffa9a2e4e98600fa20261007094834",
        phone="9876543210", entry_time=datetime(2026, 10, 7, 9, 49, 50), exit_time=None,
        is_appointment=False, is_regular=False, visitor_name="Anand", whom_to_visit="KIMS",
        company_name="KIMS", department="GENERAL MEDICINE", visit_purpose="", block="",
        floor="", room="", visitor_count="1", appointment_time="",
    )
    base.update(overrides)
    return SlipData(**base)


def test_qr_text_matches_the_android_app():
    assert slip().qr_text == "0 ffa9a2e4e98600fa20261007094834"
    assert slip(is_appointment=True).qr_text.startswith("1 ")
    assert slip(is_regular=True).qr_text.startswith("2 ")
    assert slip(is_appointment=True, is_regular=True).qr_text.startswith("1 ")  # appointment wins


def test_duration():
    s = slip(event_type="EXIT", exit_time=datetime(2026, 10, 7, 11, 15, 5))
    assert s.duration == "1 h 25 m"
    assert slip().duration == ""
    assert slip(exit_time=datetime(2026, 10, 7, 9, 0)).duration == ""   # exit before entry


def test_template_values():
    v = slip().template_values("https://x/a.pdf")
    assert v["pdf_url"] == "https://x/a.pdf"
    assert v["entry_time"] == "07-10-2026 09:49:50"
    assert v["exit_time"] == ""


def test_backoff_uses_last_value_when_attempts_run_longer(settings):
    assert [backoff_minutes(settings, a) for a in (1, 2, 3, 4, 9)] == [1, 5, 15, 15, 15]
