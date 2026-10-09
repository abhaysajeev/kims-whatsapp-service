"""Shared fixtures. Tests never touch the network: HTTP goes through FakeSession."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from app.config import PROJECT_ROOT, Settings, load_settings

TEST_ENV = {
    "DB_HOST": "127.0.0.1", "DB_PORT": "33065", "DB_NAME": "vizipluskims",
    "DB_USER": "root", "DB_PASSWORD": "rootpw",
    "ACL_AUTH_URL": "https://auth.example.test/token",
    "ACL_OPTIN_URL": "https://optin.example.test/bulk",
    "ACL_SEND_URL": "https://api.example.test/messages",
    "ACL_CLIENT_ID": "client", "ACL_USERNAME": "acl-user", "ACL_PASSWORD": "acl-secret",
    "TEMPLATE_NAME": "linkforentrypassaccess", "TEMPLATE_LANGUAGE": "en",
    "BUNNY_STORAGE_HOST": "storage.example.test", "BUNNY_STORAGE_ZONE": "zone",
    "BUNNY_ACCESS_KEY": "bunny-secret", "BUNNY_CDN_URL": "https://cdn.example.test",
    "BUNNY_ENTRY_FOLDER": "qa_entry", "BUNNY_EXIT_FOLDER": "qa_exit",
    "POLL_INTERVAL_SECONDS": "1", "MAX_ATTEMPTS": "3", "RETRY_BACKOFF_MINUTES": "1,5,15",
    "STUCK_JOB_MINUTES": "10", "DEFAULT_COUNTRY_CODE": "91", "HTTP_TIMEOUT_SECONDS": "5",
    "MAX_CONSECUTIVE_DB_FAILURES": "3", "WHATSAPP_SEND_ENABLED": "true", "PDF_OUTPUT_DIR": "", "LOG_LEVEL": "INFO",
}


@pytest.fixture
def settings(monkeypatch, tmp_path) -> Settings:
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "heartbeat"))
    return load_settings(
        env_file=None,
        mapping_file=PROJECT_ROOT / "table_mapping.yaml",
        templates_file=PROJECT_ROOT / "whatsapp_templates.yaml",
    )


class FakeResponse(requests.Response):
    def __init__(self, status: int, body: object = None):
        super().__init__()
        self.status_code = status
        if body is not None:
            self._content = (body if isinstance(body, str) else json.dumps(body)).encode()
        else:
            self._content = b""


class FakeSession:
    """Returns queued responses per URL; an Exception in the queue is raised."""

    def __init__(self):
        self.queues: dict[str, list] = {}
        self.calls: list[dict] = []

    def add(self, url: str, *responses) -> "FakeSession":
        self.queues.setdefault(url, []).extend(responses)
        return self

    def _key(self, url: str) -> str:
        """Exact URL, or a registered prefix ending in '*'."""
        if url in self.queues:
            return url
        for key in self.queues:
            if key.endswith("*") and url.startswith(key[:-1]):
                return key
        return url

    def _next(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        queue = self.queues.get(self._key(url))
        if not queue:
            raise AssertionError(f"unexpected {method} {url}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._next("PUT", url, **kwargs)

    def calls_to(self, url: str) -> list[dict]:
        return [c for c in self.calls if c["url"] == url]


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


def token_response(expires_in: int = 36000, token: str = "tok-1") -> FakeResponse:
    return FakeResponse(200, {"access_token": token, "expires_in": expires_in, "token_type": "Bearer"})


def accepted_response(response_id: str = "resp-1") -> FakeResponse:
    return FakeResponse(200, {"success": "true", "responseId": response_id})


# Body of the send request tested against ACL (phone number replaced with a dummy)
SAMPLE_REQUEST = PROJECT_ROOT / "tests" / "fixtures" / "acl_send_request.json"
