"""Classifying a provider failure into a coarse kind, and nothing more.

Ported from `server/models/error-kind.js`, whose patterns were each added in
response to a real, live failure — the comments there record which. That
accumulated knowledge is worth keeping verbatim even though the exception
objects are different in Python; what the classifier actually reads is an HTTP
status and some message text, and both providers' Python SDKs expose those in
the same shapes.

The one structural difference: Python SDK exceptions nest through `__cause__`
rather than `.cause`, and carry `status_code` (httpx-derived) alongside `status`.
Both chains are walked.

Never raises. An unclassifiable failure is `other`, which maps to a moderate
cooldown rather than the harshest one — an unrecognised failure is not evidence
of a permanent problem.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: kind -> the availability state persisted for it. The one place this mapping
#: is declared; in the Node version three call sites each carried a copy.
AVAILABILITY_STATE_FOR_KIND: dict[str, str] = {
    "quota": "quota",
    "auth": "auth",
    "no_access": "no_access",
    "network": "unreachable",
    "other": "error",
    "transient": "busy",
    "unsupported": "unsupported",
}

_NETWORK_NAMES = ("ConnectError", "ConnectTimeout", "ReadTimeout", "ConnectionRefusedError",
                  "TimeoutError", "gaierror", "APIConnectionError")
_NETWORK_CODES = {"ECONNREFUSED", "ETIMEDOUT", "ENOTFOUND"}

_TRANSIENT_TEXT = re.compile(
    r"overloaded|high demand|temporarily unavailable|service unavailable"
    r"|currently unavailable|try again later", re.I)
_UNSUPPORTED_TEXT = re.compile(
    r"no endpoints found|no allowed providers|not a valid model|model not found"
    r"|unknown model|does not support tool use|does not support tools", re.I)
_INVALID_ARGUMENT_TEXT = re.compile(r"invalid argument|invalid request", re.I)
# The safety valve: a 400 about context length is a per-TURN problem, not a
# per-model one, and must never bench the model permanently.
_CONTEXT_LENGTH_TEXT = re.compile(
    r"context length|context window|too (many|long) tokens|maximum context|token limit", re.I)


def _chain(err: BaseException | None) -> list[BaseException]:
    out: list[BaseException] = []
    current = err
    for _ in range(5):
        if current is None:
            break
        out.append(current)
        current = current.__cause__ or current.__context__
    return out


def _is_network(err: BaseException) -> bool:
    for link in _chain(err):
        if type(link).__name__ in _NETWORK_NAMES:
            return True
        code = getattr(link, "errno", None) or getattr(link, "code", None)
        if isinstance(code, str) and code in _NETWORK_CODES:
            return True
    return False


def _parsed_body(err: BaseException) -> dict[str, Any] | None:
    """Gemini's shape: the message IS a JSON document."""
    try:
        raw = str(getattr(err, "message", "") or err)
        return json.loads(raw)
    except Exception:
        return None


def find_status(err: BaseException) -> int | None:
    for link in _chain(err):
        for attr in ("status_code", "status"):
            value = getattr(link, attr, None)
            if isinstance(value, int):
                return value
        response = getattr(link, "response", None)
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
        body = _parsed_body(link)
        if isinstance(body, dict):
            inner = body.get("error")
            if isinstance(inner, dict) and isinstance(inner.get("code"), int):
                return inner["code"]
    return None


def find_message(err: BaseException) -> str:
    parts: list[str] = []
    for link in _chain(err):
        parts.append(str(link))
        body = _parsed_body(link)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            message = body["error"].get("message")
            if isinstance(message, str):
                parts.append(message)
        for attr in ("message", "body"):
            value = getattr(link, attr, None)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, dict):
                inner = value.get("error")
                if isinstance(inner, dict) and isinstance(inner.get("message"), str):
                    parts.append(inner["message"])
                elif isinstance(value.get("message"), str):
                    parts.append(value["message"])
    return " ".join(parts)


def classify_error(err: BaseException | None) -> str:
    """'quota' | 'auth' | 'no_access' | 'network' | 'transient' | 'unsupported' | 'other'."""
    if err is None:
        return "other"
    try:
        if _is_network(err):
            return "network"

        status = find_status(err)
        if status == 429:
            return "quota"
        if status == 401:
            return "auth"
        if status == 403:
            return "no_access"
        if status == 404:
            return "unsupported"
        if status in (500, 502, 503, 504, 529):
            return "transient"

        text = find_message(err)
        if re.search(r"quota|rate.?limit|usage limit", text, re.I):
            return "quota"
        # Both word orders: Gemini's real invalid-key text is "API key not
        # valid...", which an `invalid.*key` pattern alone never matches.
        if re.search(r"invalid.*key|key.*invalid|key.*not valid|unauthorized|authentication",
                     text, re.I):
            return "auth"
        if re.search(r"permission|access denied|not enabled|forbidden", text, re.I):
            return "no_access"
        if re.search(r"couldn.?t reach|connection refused|timed? ?out|not reachable", text, re.I):
            return "network"
        if _TRANSIENT_TEXT.search(text):
            return "transient"
        if _UNSUPPORTED_TEXT.search(text):
            return "unsupported"
        if _INVALID_ARGUMENT_TEXT.search(text) and not _CONTEXT_LENGTH_TEXT.search(text):
            return "unsupported"
        return "other"
    except Exception:
        return "other"


def availability_state_for(err: BaseException | None) -> str:
    return AVAILABILITY_STATE_FOR_KIND.get(classify_error(err), "error")
