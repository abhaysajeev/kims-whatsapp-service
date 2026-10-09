"""ACL WhatsApp API: token cache, opt-in and template send."""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

from app.config import AclSettings, TemplateSpec
from app.errors import (
    PermanentError,
    ServiceError,
    classify_status,
    network_error,
    short_body,
)
from app.logging_setup import fields

log = logging.getLogger(__name__)

OPTIN_OK = (200, 201, 202)
DEFAULT_TOKEN_SECONDS = 300
_WHITESPACE = re.compile(r"\s+")


class TokenProvider:
    """Caches the bearer token in memory and renews it shortly before expiry.

    Fetched lazily (an idle worker makes no auth calls). Any failure is a
    ServiceError: the job is put back and the worker pauses, so wrong
    credentials never burn job attempts or hammer the auth server (Keycloak
    can lock an account after repeated failed logins).
    """

    def __init__(self, settings: AclSettings, session: requests.Session, timeout: int,
                 clock: Callable[[], float] = time.monotonic):
        self.s = settings
        self.session = session
        self.timeout = (10, timeout)
        self.clock = clock
        self._token: str | None = None
        self._renew_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token is None or self.clock() >= self._renew_at:
                self._fetch()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _fetch(self) -> None:
        data = {
            "grant_type": "password",
            "client_id": self.s.client_id,
            "username": self.s.username,
            "password": self.s.password.get(),
        }
        try:
            response = self.session.post(self.s.auth_url, data=data, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ServiceError(f"ACL auth unreachable: {type(exc).__name__}") from None

        if response.status_code != 200:
            reason = ""
            try:
                body = response.json()
                reason = f", {body.get('error', '')}: {body.get('error_description', '')}"
            except ValueError:
                pass
            raise ServiceError(
                f"ACL authentication failed (HTTP {response.status_code}{reason}); "
                "check ACL_CLIENT_ID / ACL_USERNAME / ACL_PASSWORD",
                response.status_code,
            )
        try:
            body = response.json()
            token = body["access_token"]
        except (ValueError, KeyError, TypeError):
            raise ServiceError("ACL auth response has no access_token") from None

        try:
            lifetime = int(body.get("expires_in") or DEFAULT_TOKEN_SECONDS)
        except (TypeError, ValueError):
            lifetime = DEFAULT_TOKEN_SECONDS
        margin = min(300.0, lifetime * 0.1)
        self._token = token
        self._renew_at = self.clock() + lifetime - margin
        log.info("ACL token renewed", extra=fields(valid_seconds=lifetime,
                                                   renew_in_seconds=int(lifetime - margin)))


def single_line(text: str) -> str:
    """WhatsApp rejects text parameters containing newlines, tabs or 4+ spaces."""
    return _WHITESPACE.sub(" ", text or "").strip()


def build_payload(template: TemplateSpec, to: str, values: dict[str, str],
                  message_id: str, transaction_id: str) -> dict:
    components = []
    for comp in template.components:
        entry: dict = {"type": comp.type}
        if comp.sub_type:
            entry["sub_type"] = comp.sub_type
        if comp.index is not None:
            entry["index"] = comp.index
        entry["parameters"] = [
            {"type": p.type, "text": single_line(values.get(p.source, ""))}
            for p in comp.parameters
        ]
        components.append(entry)
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": {
            "name": template.name,
            "language": {"policy": template.language_policy, "code": template.language},
            "components": components,
        },
        "metadata": {"messageId": message_id, "transactionId": transaction_id},
    }


@dataclass(frozen=True)
class SendResult:
    response_id: str | None
    http_status: int


class AclClient:
    def __init__(self, settings: AclSettings, session: requests.Session, timeout: int,
                 tokens: TokenProvider):
        self.s = settings
        self.session = session
        self.timeout = (10, timeout)
        self.tokens = tokens

    def opt_in(self, phone: str) -> int:
        """Opt one normalised number in. Returns the HTTP status on success."""
        headers = {
            "user": self.s.username,
            "pass": self.s.password.get(),
            "Content-Type": "application/json",
        }
        try:
            response = self.session.post(self.s.optin_url, json={"msisdnList": [phone]},
                                         headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise network_error("ACL opt-in", exc) from None
        if response.status_code not in OPTIN_OK:
            raise classify_status("ACL opt-in", response)
        return response.status_code

    def send(self, payload: dict) -> SendResult:
        """Send a template message; on 401 renew the token once and retry."""
        response = self._post_send(payload)
        if response.status_code == 401:
            log.warning("ACL send returned 401; renewing token and retrying once")
            self.tokens.invalidate()
            response = self._post_send(payload)
            if response.status_code == 401:
                raise ServiceError("ACL send still unauthorised after renewing the token", 401)

        if response.status_code != 200:
            # Only 401 is a credentials problem here; 403 is about the message itself
            raise classify_status("ACL send", response, credentials_statuses=frozenset())

        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or str(body.get("success")).lower() != "true":
            # ACL answered 200 but did not confirm. Retrying could duplicate the
            # message, so stop here and keep the body for investigation.
            raise PermanentError(f"ACL send unexpected response: {short_body(response)}", 200)
        response_id = body.get("responseId")
        return SendResult(str(response_id) if response_id else None, 200)

    def _post_send(self, payload: dict) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {self.tokens.get()}",
            "Content-Type": "application/json",
        }
        try:
            return self.session.post(self.s.send_url, json=payload, headers=headers,
                                     timeout=self.timeout)
        except requests.RequestException as exc:
            raise network_error("ACL send", exc) from None
