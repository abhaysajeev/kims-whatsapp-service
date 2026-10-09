"""Logging to stdout (Docker rotates it), in one of two formats.

LOG_FORMAT=pretty  (default) one readable block per event, times in IST:

    2026-10-07 14:41:36 IST  INFO   message accepted by ACL
        job              1
        event            ENTRY
        phone            91XXXXXXX366

LOG_FORMAT=line    one key=value line per event, times in UTC (grep / log tools)

Usage:  log.info("job accepted", extra=fields(job=12, event="ENTRY"))
Never pass secrets; pass phones through phone.mask().
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone

LOG_FORMATS = ("pretty", "line")
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def fields(**kwargs) -> dict:
    return {"fields": kwargs}


def _record_fields(record: logging.LogRecord) -> dict:
    return {k: v for k, v in getattr(record, "fields", {}).items() if v is not None}


def _fmt(value: object) -> str:
    text = str(value)
    if text == "" or any(c in text for c in ' ="'):
        return '"' + text.replace('"', "'") + '"'
    return text


class KeyValueFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            f"ts={self.formatTime(record, '%Y-%m-%dT%H:%M:%SZ')}",
            f"level={record.levelname}",
            f"logger={record.name}",
            f"msg={_fmt(record.getMessage())}",
        ]
        for key, value in _record_fields(record).items():
            parts.append(f"{key}={_fmt(value)}")
        if record.exc_info:
            parts.append(f"exc={_fmt(self.formatException(record.exc_info).splitlines()[-1])}")
        return " ".join(parts)


class PrettyFormatter(logging.Formatter):
    """Header line (IST time, level, message) and one indented line per field."""

    INDENT = "    "

    def format(self, record: logging.LogRecord) -> str:
        when = datetime.fromtimestamp(record.created, IST).strftime("%Y-%m-%d %H:%M:%S IST")
        lines = [f"{when}  {record.levelname:<8} {record.getMessage()}"]
        data = _record_fields(record)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info).splitlines()[-1]
        if data:
            width = max(len(k) for k in data) + 2
            lines += [f"{self.INDENT}{key:<{width}}{value}" for key, value in data.items()]
        return "\n".join(lines) + "\n"   # blank line between events


def setup_logging(level: str, fmt: str = "pretty") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(PrettyFormatter() if fmt == "pretty" else KeyValueFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # requests/urllib3 debug lines can include headers; keep them quiet
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # WeasyPrint logs every rendering step at INFO
    logging.getLogger("weasyprint").setLevel(logging.ERROR)
    logging.getLogger("fontTools").setLevel(logging.WARNING)
