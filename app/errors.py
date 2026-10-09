"""How a failure affects a job.

RetryableError  temporary (network, 5xx, 429): retry with backoff until MAX_ATTEMPTS
PermanentError  will not succeed by retrying (invalid phone, 4xx): FAILED now
ServiceError    our side is broken (ACL login, Bunny key): the job is put back
                WITHOUT using an attempt and the worker pauses, so a bad
                credential cannot push every queued job to FAILED
"""
from __future__ import annotations

import re

import requests


class JobError(Exception):
    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class RetryableError(JobError):
    pass


class PermanentError(JobError):
    pass


class ServiceError(JobError):
    pass


_LONG_DIGITS = re.compile(r"\d{8,}")


def short_body(response: requests.Response, limit: int = 300) -> str:
    """Response body for error messages: truncated, long digit runs (phones) masked."""
    text = (response.text or "").replace("\n", " ").strip()
    return _LONG_DIGITS.sub("<digits>", text)[:limit]


def classify_status(service: str, response: requests.Response,
                    credentials_statuses: frozenset[int] = frozenset({401, 403})) -> JobError:
    """Turn a non-success HTTP response into the right error type."""
    status = response.status_code
    message = f"{service} HTTP {status}: {short_body(response)}"
    if status in credentials_statuses:
        return ServiceError(message, status)
    if status >= 500 or status in (408, 429):
        return RetryableError(message, status)
    return PermanentError(message, status)


def network_error(service: str, exc: requests.RequestException) -> RetryableError:
    return RetryableError(f"{service} unreachable: {type(exc).__name__}")
