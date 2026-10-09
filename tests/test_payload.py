import json
import uuid

from app.acl import build_payload, single_line
from app.config import TemplateComponent, TemplateParameter, TemplateSpec
from app.uuid7 import uuid7

from tests.conftest import SAMPLE_REQUEST

URL = "https://cdn.example.test/qa_entry/abc.pdf"


def _payload(settings, url=URL):
    return build_payload(settings.template, "919876543210", {"pdf_url": url}, "m-id", "t-id")


def test_payload_matches_the_tested_curl_request(settings):
    """Same structure as the request body tested with ACL (tests/fixtures/acl_send_request.json)."""
    sample = json.loads(SAMPLE_REQUEST.read_text())
    built = _payload(settings)

    def shape(obj):
        if isinstance(obj, dict):
            return {k: shape(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [shape(v) for v in obj]
        return type(obj).__name__

    assert shape(built) == shape(sample)
    assert built["template"]["name"] == sample["template"]["name"]
    assert built["template"]["language"] == sample["template"]["language"]
    assert built["template"]["components"][0]["type"] == "body"


def test_payload_values(settings):
    p = _payload(settings)
    assert p["to"] == "919876543210"
    assert p["template"]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": URL}]}
    ]
    assert p["metadata"] == {"messageId": "m-id", "transactionId": "t-id"}
    assert "button" not in json.dumps(p)


def test_link_is_forced_onto_one_line(settings):
    p = _payload(settings, url=" https://x.test/a.pdf\n\t ")
    assert p["template"]["components"][0]["parameters"][0]["text"] == "https://x.test/a.pdf"


def test_single_line_collapses_whitespace():
    assert single_line("a\n\tb    c ") == "a b c"
    assert single_line(None) == ""


def test_other_template_layouts_need_no_code_change():
    spec = TemplateSpec(
        name="with_button", language="en", language_policy="deterministic",
        components=(
            TemplateComponent("body", (TemplateParameter("text", "visitor_name"),)),
            TemplateComponent("button", (TemplateParameter("text", "pdf_url"),),
                              sub_type="url", index="0"),
        ),
    )
    p = build_payload(spec, "919876543210", {"visitor_name": "Anand", "pdf_url": URL}, "m", "t")
    assert p["template"]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Anand"}]},
        {"type": "button", "sub_type": "url", "index": "0",
         "parameters": [{"type": "text", "text": URL}]},
    ]


def test_uuid7_format_and_order():
    a, b = uuid7(unix_ms=1_700_000_000_000), uuid7(unix_ms=1_700_000_000_001)
    for u in (a, b):
        assert isinstance(u, uuid.UUID)
        assert u.version == 7
        assert u.variant == uuid.RFC_4122
    assert str(a) < str(b)
    assert (a.int >> 80) == 1_700_000_000_000
    assert len({str(uuid7()) for _ in range(1000)}) == 1000
