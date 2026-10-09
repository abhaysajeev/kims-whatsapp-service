"""UUID version 7 (RFC 9562): 48-bit Unix milliseconds + random bits.

Python's uuid module only gains uuid7() in 3.14.
"""
from __future__ import annotations

import os
import time
import uuid


def uuid7(unix_ms: int | None = None) -> uuid.UUID:
    ms = int(time.time() * 1000) if unix_ms is None else unix_ms
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand & 0xFFF                      # 12 bits
    rand_b = (rand >> 12) & ((1 << 62) - 1)    # 62 bits
    value = (
        (ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76          # version
        | rand_a << 64
        | 0b10 << 62         # RFC 9562 variant
        | rand_b
    )
    return uuid.UUID(int=value)
