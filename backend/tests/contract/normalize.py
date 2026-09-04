"""Normalisation for contract replay — deliberately narrow, and self-reporting.

The whole value of the record-and-compare harness rests on this file. An
over-eager normaliser silently hides exactly the differences the suite exists to
catch, and it does so invisibly: every test still passes, and nobody finds out
until weeks later. So two rules govern everything here:

1. Nothing is normalised by guessing at a value's shape. A field is normalised
   only because it appears in one of the explicit lists below, and each entry
   carries the reason it cannot be compared literally.

2. Every normalisation is COUNTED and returned to the caller, so a test can
   assert on how much was waved away. A fixture whose comparison only passes
   because 40 values got normalised is not really passing, and the count is what
   makes that visible instead of invisible.
"""

from __future__ import annotations

import re
from typing import Any

# Response headers that carry real contract meaning and MUST match. Anything not
# in this list is ignored — but ignoring is the default only for transport
# plumbing, never for something a client's behaviour depends on.
CONTRACT_HEADERS = {
    "content-type",
    # The artifacts route's forced-download fix. A body-only comparison would
    # not notice this regressing, and it is a real, previously-exploited
    # stored-XSS hole.
    "content-disposition",
    "x-content-type-options",
    "content-security-policy",
    "location",
    "cache-control",
}

# Transport/server headers deliberately NOT compared, with why:
#   x-powered-by   Express-specific; FastAPI will never emit it, by design
#   date           wall-clock, differs every request
#   etag           Express's own body hash; FastAPI does not emit one
#   content-length derived from the body, which is compared directly anyway
#   connection / keep-alive / transfer-encoding  per-connection plumbing
#   server         names the server software, which is the thing changing

# Body fields whose value cannot be stable between two runs, by exact key name.
VOLATILE_KEYS = {
    # timestamps written at request time
    "createdAt", "updatedAt", "created_at", "updated_at", "recordedAt",
    "startedAt", "finishedAt", "checkedAt", "addedAt", "changedAt", "at", "ts",
    "lastRunAt", "nextRunAt", "lastSeenAt", "since", "timestamp",
    # ids minted per request
    "id", "conversationId", "conversation_id", "runId", "jobId", "sessionId",
    # host-specific facts that differ between the recording machine and the
    # replaying one, and are not part of the API's shape
    "uptime", "pid", "port", "cwd", "platform", "hostname", "version",
    "loadAverage", "cpuPercent", "freeMemory", "totalMemory",
}

PLACEHOLDER = "<normalised>"


class Normaliser:
    """Applies the rules above, counting every substitution it makes."""

    def __init__(self) -> None:
        self.count = 0
        self.fields: list[str] = []

    def _mark(self, path: str) -> str:
        self.count += 1
        self.fields.append(path)
        return PLACEHOLDER

    def body(self, value: Any, path: str = "") -> Any:
        if isinstance(value, dict):
            out = {}
            for key, val in value.items():
                child = f"{path}.{key}" if path else key
                if key in VOLATILE_KEYS and not isinstance(val, (dict, list)):
                    out[key] = self._mark(child)
                else:
                    out[key] = self.body(val, child)
            return out
        if isinstance(value, list):
            return [self.body(v, f"{path}[{i}]") for i, v in enumerate(value)]
        return value

    def headers(self, headers: dict[str, Any]) -> dict[str, str]:
        """Reduce headers to the contract-bearing subset, lower-cased.

        content-type is compared without its charset parameter: FastAPI and
        Express spell the same media type differently ("application/json" vs
        "application/json; charset=utf-8") while meaning exactly the same thing
        to a browser.
        """
        out: dict[str, str] = {}
        for key, val in headers.items():
            low = key.lower()
            if low not in CONTRACT_HEADERS:
                continue
            text = str(val)
            if low == "content-type":
                text = text.split(";")[0].strip().lower()
            out[low] = text
        return out


def normalise_exchange(status: int, headers: dict, body: Any) -> tuple[dict, Normaliser]:
    n = Normaliser()
    return (
        {"status": status, "headers": n.headers(headers), "body": n.body(body)},
        n,
    )
