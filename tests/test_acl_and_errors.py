"""Token cache, opt-in, send, Bunny upload and the retry/error classification."""
import pytest
import requests

from app.acl import AclClient, TokenProvider
from app.bunny import BunnyClient
from app.errors import PermanentError, RetryableError, ServiceError

from tests.conftest import FakeResponse, accepted_response, token_response

AUTH = "https://auth.example.test/token"
OPTIN = "https://optin.example.test/bulk"
SEND = "https://api.example.test/messages"
PUT = "https://storage.example.test/zone/qa_entry/f.pdf"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tokens(settings, session, clock):
    return TokenProvider(settings.acl, session, 5, clock=clock)


@pytest.fixture
def acl(settings, session, tokens):
    return AclClient(settings.acl, session, 5, tokens)


# --- token ------------------------------------------------------------------

def test_token_is_cached_until_shortly_before_expiry(session, tokens, clock):
    session.add(AUTH, token_response(36000, "tok-1"), token_response(36000, "tok-2"))
    assert tokens.get() == "tok-1"
    clock.now += 36000 - 301          # still before the 5-minute margin
    assert tokens.get() == "tok-1"
    clock.now += 2                    # inside the margin -> renew
    assert tokens.get() == "tok-2"
    assert len(session.calls_to(AUTH)) == 2


def test_token_request_uses_password_grant(session, tokens):
    session.add(AUTH, token_response())
    tokens.get()
    sent = session.calls_to(AUTH)[0]["data"]
    assert sent == {"grant_type": "password", "client_id": "client",
                    "username": "acl-user", "password": "acl-secret"}


def test_short_lived_token_renews_at_ten_percent_margin(session, tokens, clock):
    session.add(AUTH, token_response(100, "a"), token_response(100, "b"))
    tokens.get()
    clock.now += 89
    assert tokens.get() == "a"
    clock.now += 2
    assert tokens.get() == "b"


@pytest.mark.parametrize("response", [
    FakeResponse(401, {"error": "invalid_grant", "error_description": "Invalid user credentials"}),
    FakeResponse(400, {"error": "invalid_client"}),
    FakeResponse(503, "busy"),
    FakeResponse(200, {"no_token": True}),
    requests.ConnectionError("down"),
])
def test_any_auth_failure_is_a_service_error(session, tokens, response):
    session.add(AUTH, response)
    with pytest.raises(ServiceError):
        tokens.get()


def test_auth_error_message_never_contains_the_password(session, tokens):
    session.add(AUTH, FakeResponse(401, {"error": "invalid_grant"}))
    with pytest.raises(ServiceError) as info:
        tokens.get()
    assert "acl-secret" not in str(info.value)
    assert "invalid_grant" in str(info.value)


# --- send -------------------------------------------------------------------

def test_send_accepted(session, acl):
    session.add(AUTH, token_response())
    session.add(SEND, accepted_response("r-42"))
    result = acl.send({"x": 1})
    assert result.response_id == "r-42"
    call = session.calls_to(SEND)[0]
    assert call["headers"]["Authorization"] == "Bearer tok-1"
    assert call["json"] == {"x": 1}


def test_send_401_renews_token_once_and_retries_same_payload(session, acl):
    session.add(AUTH, token_response(token="old"), token_response(token="new"))
    session.add(SEND, FakeResponse(401, "expired"), accepted_response("r-1"))
    assert acl.send({"x": 1}).response_id == "r-1"
    sends = session.calls_to(SEND)
    assert [c["headers"]["Authorization"] for c in sends] == ["Bearer old", "Bearer new"]
    assert sends[0]["json"] == sends[1]["json"]


def test_send_401_twice_is_a_service_error(session, acl):
    session.add(AUTH, token_response(), token_response())
    session.add(SEND, FakeResponse(401), FakeResponse(401))
    with pytest.raises(ServiceError):
        acl.send({})


@pytest.mark.parametrize("response, expected", [
    (FakeResponse(500, "oops"), RetryableError),
    (FakeResponse(502), RetryableError),
    (FakeResponse(503), RetryableError),
    (FakeResponse(429, "slow down"), RetryableError),
    (FakeResponse(408), RetryableError),
    (requests.ConnectionError("reset"), RetryableError),
    (requests.Timeout("slow"), RetryableError),
    (FakeResponse(400, {"error": "bad template"}), PermanentError),
    (FakeResponse(403, "forbidden"), PermanentError),
    (FakeResponse(404), PermanentError),
    (FakeResponse(422), PermanentError),
    (FakeResponse(200, {"success": "false", "error": "x"}), PermanentError),
    (FakeResponse(200, "not json"), PermanentError),
])
def test_send_error_classification(session, acl, response, expected):
    session.add(AUTH, token_response())
    session.add(SEND, response)
    with pytest.raises(expected) as info:
        acl.send({})
    assert type(info.value) is expected


def test_error_messages_mask_phone_numbers(session, acl):
    session.add(AUTH, token_response())
    session.add(SEND, FakeResponse(400, {"error": "bad number 919876543210"}))
    with pytest.raises(PermanentError) as info:
        acl.send({})
    assert "919876543210" not in str(info.value)


# --- opt-in -----------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 201, 202])
def test_optin_success_statuses(session, acl, status):
    session.add(OPTIN, FakeResponse(status, {}))
    assert acl.opt_in("919876543210") == status
    call = session.calls_to(OPTIN)[0]
    assert call["json"] == {"msisdnList": ["919876543210"]}
    assert call["headers"]["user"] == "acl-user"
    assert call["headers"]["pass"] == "acl-secret"
    assert "Authorization" not in call["headers"]
    assert not session.calls_to(AUTH)   # opt-in never needs the bearer token


@pytest.mark.parametrize("response, expected", [
    (FakeResponse(401), ServiceError),
    (FakeResponse(403), ServiceError),
    (FakeResponse(500), RetryableError),
    (requests.ConnectionError("x"), RetryableError),
    (FakeResponse(400, "invalid msisdn"), PermanentError),
])
def test_optin_error_classification(session, acl, response, expected):
    session.add(OPTIN, response)
    with pytest.raises(expected):
        acl.opt_in("919876543210")


# --- bunny ------------------------------------------------------------------

def test_bunny_upload(settings, session):
    session.add(PUT, FakeResponse(201, {"HttpCode": 201}))
    bunny = BunnyClient(settings.bunny, session, 5)
    assert bunny.upload(b"%PDF-1.4 x", "qa_entry", "f.pdf") == "https://cdn.example.test/qa_entry/f.pdf"
    headers = session.calls_to(PUT)[0]["headers"]
    assert headers["AccessKey"] == "bunny-secret"
    assert len(headers["Checksum"]) == 64


@pytest.mark.parametrize("response, expected", [
    (FakeResponse(401), ServiceError),
    (FakeResponse(500), RetryableError),
    (requests.Timeout("x"), RetryableError),
    (FakeResponse(400), PermanentError),
])
def test_bunny_error_classification(settings, session, response, expected):
    session.add(PUT, response)
    with pytest.raises(expected):
        BunnyClient(settings.bunny, session, 5).upload(b"%PDF", "qa_entry", "f.pdf")


def test_bunny_rejects_empty_pdf(settings, session):
    with pytest.raises(PermanentError):
        BunnyClient(settings.bunny, session, 5).upload(b"", "qa_entry", "f.pdf")
