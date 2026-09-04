"""Small primitives that must behave exactly like their JavaScript originals.

These exist because the Python backend and the Node backend read and write the
same database rows, the same JSON payload strings and the same id formats. A
"close enough" equivalent here shows up much later as an id that sorts wrongly,
a payload that diffs against itself, or a timestamp that compares incorrectly
against rows the other implementation wrote.

Each function names the JS expression it reproduces, so the correspondence can be
checked rather than trusted.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from typing import Any

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def base36(n: int) -> str:
    """`Number.prototype.toString(36)` for a non-negative integer."""
    if n == 0:
        return "0"
    digits = []
    while n:
        n, rem = divmod(n, 36)
        digits.append(_BASE36[rem])
    return "".join(reversed(digits))


def now_ms() -> int:
    """`Date.now()` — integer milliseconds since the epoch."""
    return int(time.time() * 1000)


def now_iso() -> str:
    """`new Date().toISOString()`.

    Always UTC, always exactly three fractional digits, always a trailing 'Z'.
    Python's own isoformat() gives six fractional digits and '+00:00', which
    would sort and compare differently against every row the Node app wrote.
    """
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{now.microsecond // 1000:03d}Z"


def random_suffix(length: int = 6) -> str:
    """`Math.random().toString(36).slice(2, 2 + length)`.

    Not the same random VALUES, which would be meaningless to reproduce — the
    same alphabet and length, so ids from either implementation are
    indistinguishable in shape and collide no more often.
    """
    return "".join(random.choice(_BASE36) for _ in range(length))


def compact_json(value: Any) -> str:
    """`JSON.stringify(value)` with no spacing.

    Python's json.dumps defaults to ', ' and ': ' separators; JavaScript uses
    none. These strings are stored in the database and read back by both
    implementations, so a mismatch would make identical payloads compare as
    different rows.
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
