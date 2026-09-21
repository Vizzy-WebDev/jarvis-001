"""Small primitives that reproduce the stored formats exactly.

Database rows, JSON payload strings and ids written earlier are already on disk in these
formats, so a "close enough" equivalent here shows up much later as an id that sorts wrongly,
a payload that diffs against itself, or a timestamp that compares incorrectly against older
rows. Each function documents the exact format it produces.
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
    """The stored timestamp format.

    Always UTC, always exactly three fractional digits, always a trailing 'Z'.
    Python's own isoformat() gives six fractional digits and '+00:00', which
    would sort and compare differently against every row already stored.
    """
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{now.microsecond // 1000:03d}Z"


def to_iso_z(moment: datetime) -> str:
    """Any datetime in the stored timestamp format: UTC, three fractional
    digits, trailing Z. A naive datetime is taken as local time, which is what
    every scheduling calculation in this app produces."""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    utc = moment.astimezone(timezone.utc)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond // 1000:03d}Z"


def random_suffix(length: int = 6) -> str:
    """A random base-36 id suffix.

    The same alphabet and length as the ids already stored, so old and new ids are
    indistinguishable in shape and collide no more often.
    """
    return "".join(random.choice(_BASE36) for _ in range(length))


def compact_json(value: Any) -> str:
    """JSON with no spacing.

    Python's json.dumps defaults to ', ' and ': ' separators; the stored payloads use
    none, so a mismatch would make identical payloads compare as different rows.
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
