"""Phone number normalisation for ACL: digits only, country code first, no '+'.

    "9876543210"        -> "919876543210"
    "+91 98765-43210"   -> "919876543210"
    "0091 9876543210"   -> "919876543210"
    "09876543210"       -> "919876543210"
"""
from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[\s\-().]")
NATIONAL_LENGTH = 10


class InvalidPhone(ValueError):
    pass


def normalize(raw: object, country_code: str = "91") -> str:
    if raw is None or not str(raw).strip():
        raise InvalidPhone("empty")
    digits = _SEPARATORS.sub("", str(raw).strip())

    international = False
    if digits.startswith("+"):
        digits, international = digits[1:], True
    elif digits.startswith("00"):
        digits, international = digits[2:], True

    if not digits.isdigit():
        raise InvalidPhone("contains characters other than digits")

    if not international:
        # National forms: trunk-prefixed 0XXXXXXXXXX or bare XXXXXXXXXX
        if len(digits) == NATIONAL_LENGTH + 1 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == NATIONAL_LENGTH:
            digits = country_code + digits

    if len(digits) < NATIONAL_LENGTH:
        raise InvalidPhone(f"too short ({len(digits)} digits)")
    if not digits.startswith(country_code):
        raise InvalidPhone(f"unsupported country code (only +{country_code} is accepted)")
    national = digits[len(country_code):]
    if len(national) != NATIONAL_LENGTH:
        raise InvalidPhone(f"wrong length ({len(national)} digits after the country code, expected {NATIONAL_LENGTH})")
    if country_code == "91" and national[0] not in "6789":
        raise InvalidPhone("not an Indian mobile number (must start with 6, 7, 8 or 9)")
    return digits


def mask(phone: object) -> str:
    """For logs: keep the first 2 and last 3 digits."""
    text = "" if phone is None else str(phone).strip()
    if len(text) <= 5:
        return "*" * len(text)
    return text[:2] + "X" * (len(text) - 5) + text[-3:]
