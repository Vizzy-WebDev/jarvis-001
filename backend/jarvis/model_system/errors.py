"""Normalizing a provider failure into one small, stable vocabulary (§31).

Every adapter raises whatever its own SDK raised; nothing downstream — the
gateway, `ai/fallback.py`, `ai/health.py`, a route reporting a failure to the
frontend — should ever need to know which SDK that was. `classify()` is the
one function that reads a raw exception and answers which of these it is.

**These patterns are hard-won, not guessed.** What an SDK actually puts in a
status code or an error string was confirmed against the real, installed
SDKs and real failures, not assumed from documentation — Gemini's own error
`.message` is a raw JSON document; a message pairing a field name with
"invalid request" is how a REFUSED PARAMETER and a genuinely broken model
read identically unless the parameter name is checked for specifically.
Getting a classification wrong here doesn't just misname an error — it
decides whether `ai/health.py` benches a model that was never actually
broken, or keeps offering one that reliably fails.

`benches_the_model()` is the one question `ai/fallback.py` actually asks:
whether a failure is evidence about the MODEL (bench it) or about this one
REQUEST (try the next candidate, or the same model again later, without
holding this against it).
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any


class ErrorKind(Enum):
    AUTHENTICATION = "authentication"
    PERMISSION_DENIED = "permission_denied"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_PARAMETER = "unsupported_parameter"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONTEXT_EXCEEDED = "context_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MODEL_UNAVAILABLE = "model_unavailable"
    CONTENT_POLICY = "content_policy"
    NETWORK = "network"
    UNKNOWN = "unknown"


#: Kinds that say something about the MODEL rather than about this one
#: request. `ai/health.py` benches a model only for one of these — a
#: request-level failure benched anyway removes a working model from the
#: roster for a reason the next request would not reproduce.
MODEL_LEVEL_KINDS = frozenset({
    ErrorKind.AUTHENTICATION, ErrorKind.PERMISSION_DENIED, ErrorKind.RATE_LIMIT,
    ErrorKind.PROVIDER_UNAVAILABLE, ErrorKind.MODEL_UNAVAILABLE,
    ErrorKind.UNSUPPORTED_CAPABILITY, ErrorKind.NETWORK, ErrorKind.UNKNOWN,
})

#: Kinds a bounded retry of the SAME deployment is worth attempting for —
#: transient by nature. See `ai/fallback.py`.
RETRYABLE_KINDS = frozenset({
    ErrorKind.RATE_LIMIT, ErrorKind.TIMEOUT, ErrorKind.PROVIDER_UNAVAILABLE,
    ErrorKind.NETWORK,
})

#: The parameter names Jarvis sends to control reasoning, across all three
#: wire formats this build speaks. Read off the installed SDKs rather than
#: remembered: `reasoning_effort` (OpenAI-shaped), `thinking`/`thinking_*`
#: (Anthropic/Gemini).
REASONING_PARAMETER_NAMES: tuple[str, ...] = (
    "reasoning_effort", "thinking_config", "thinking_level", "thinking_budget",
    "thinking", "reasoning", "response_format",
)

_PARAMETER_REFUSAL_TEXT = re.compile(
    r"unrecognized request argument|unknown parameter|unsupported parameter"
    r"|extra inputs are not permitted|unexpected keyword|unknown field"
    r"|unknown name|no such (field|parameter|argument)"
    r"|not a valid (field|argument|parameter)"
    r"|does not support (extended )?thinking|is not supported",
    re.I)

_NETWORK_NAMES = ("ConnectError", "ConnectionRefusedError", "gaierror", "APIConnectionError")
_NETWORK_CODES = {"ECONNREFUSED", "ENOTFOUND"}
_TIMEOUT_NAMES = ("ConnectTimeout", "ReadTimeout", "TimeoutError")

_TRANSIENT_TEXT = re.compile(
    r"overloaded|high demand|temporarily unavailable|service unavailable"
    r"|currently unavailable|try again later", re.I)
_MODEL_UNAVAILABLE_TEXT = re.compile(
    r"no endpoints found|no allowed providers|not a valid model|model not found|unknown model",
    re.I)
_UNSUPPORTED_CAPABILITY_TEXT = re.compile(
    r"does not support tool use|does not support tools|does not support (image|vision|audio|video)",
    re.I)
_CONTENT_POLICY_TEXT = re.compile(r"content polic|safety system|flagged as|blocked by safety", re.I)
_CONTEXT_LENGTH_TEXT = re.compile(
    r"context length|context window|too (many|long) tokens|maximum context|token limit", re.I)
_INVALID_ARGUMENT_TEXT = re.compile(r"invalid argument|invalid request", re.I)


def _chain(err: BaseException | None) -> list[BaseException]:
    out: list[BaseException] = []
    current = err
    for _ in range(5):
        if current is None:
            break
        out.append(current)
        current = current.__cause__ or current.__context__
    return out


def _parsed_body(err: BaseException) -> dict[str, Any] | None:
    """Gemini's shape: the message IS a JSON document."""
    try:
        return json.loads(str(getattr(err, "message", "") or err))
    except Exception:  # noqa: BLE001
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


def _is_network(err: BaseException) -> bool:
    for link in _chain(err):
        if type(link).__name__ in _NETWORK_NAMES:
            return True
        code = getattr(link, "errno", None) or getattr(link, "code", None)
        if isinstance(code, str) and code in _NETWORK_CODES:
            return True
    return False


def _is_timeout(err: BaseException) -> bool:
    return any(type(link).__name__ in _TIMEOUT_NAMES for link in _chain(err))


def refused_parameter(err: BaseException | None,
                      names: tuple[str, ...] = REASONING_PARAMETER_NAMES) -> str | None:
    """The parameter name this error is complaining about, or `None`.

    Requires BOTH a refusal-shaped phrase AND the name of a parameter this
    system actually sends — "is not supported" alone appears in plenty of
    errors that mean the model itself is unusable, and reading one of those
    as a parameter problem would keep a dead model in rotation forever.
    """
    if err is None:
        return None
    try:
        text = find_message(err)
        if not text or not _PARAMETER_REFUSAL_TEXT.search(text):
            return None
        for name in names:
            if re.search(rf"\b{re.escape(name)}\b", text, re.I):
                return name
        return None
    except Exception:  # noqa: BLE001
        return None


def classify(err: BaseException | None) -> ErrorKind:
    """Never raises. An unclassifiable failure is `UNKNOWN`, which still
    benches the model (see `MODEL_LEVEL_KINDS`) but on a moderate cooldown,
    not the harshest one — an unrecognised failure is not evidence of a
    permanent problem."""
    if err is None:
        return ErrorKind.UNKNOWN
    try:
        if _is_timeout(err):
            return ErrorKind.TIMEOUT
        if _is_network(err):
            return ErrorKind.NETWORK
        # Checked before anything status/text-based: a refusal of one
        # parameter is a fact about THIS request, and every classifier below
        # it would read the same message as a fact about the model.
        if refused_parameter(err):
            return ErrorKind.UNSUPPORTED_PARAMETER

        status = find_status(err)
        text = find_message(err)

        if _CONTEXT_LENGTH_TEXT.search(text):
            return ErrorKind.CONTEXT_EXCEEDED
        if status == 429:
            return ErrorKind.RATE_LIMIT
        if status == 401:
            return ErrorKind.AUTHENTICATION
        if status == 403:
            return ErrorKind.PERMISSION_DENIED
        if status == 404:
            return ErrorKind.MODEL_UNAVAILABLE
        if status in (500, 502, 503, 504, 529):
            return ErrorKind.PROVIDER_UNAVAILABLE

        if re.search(r"quota|rate.?limit|usage limit", text, re.I):
            return ErrorKind.RATE_LIMIT
        # Both word orders: Gemini's real invalid-key text is "API key not
        # valid...", which an `invalid.*key` pattern alone never matches.
        if re.search(r"invalid.*key|key.*invalid|key.*not valid|unauthorized|authentication",
                     text, re.I):
            return ErrorKind.AUTHENTICATION
        if re.search(r"permission|access denied|not enabled|forbidden", text, re.I):
            return ErrorKind.PERMISSION_DENIED
        if re.search(r"couldn.?t reach|connection refused|timed? ?out|not reachable", text, re.I):
            return ErrorKind.NETWORK
        if _CONTENT_POLICY_TEXT.search(text):
            return ErrorKind.CONTENT_POLICY
        if _TRANSIENT_TEXT.search(text):
            return ErrorKind.PROVIDER_UNAVAILABLE
        if _MODEL_UNAVAILABLE_TEXT.search(text):
            return ErrorKind.MODEL_UNAVAILABLE
        if _UNSUPPORTED_CAPABILITY_TEXT.search(text):
            return ErrorKind.UNSUPPORTED_CAPABILITY
        if _INVALID_ARGUMENT_TEXT.search(text):
            return ErrorKind.INVALID_REQUEST
        return ErrorKind.UNKNOWN
    except Exception:  # noqa: BLE001 — a classifier must never itself fail a turn
        return ErrorKind.UNKNOWN


def benches_the_model(kind: ErrorKind) -> bool:
    return kind in MODEL_LEVEL_KINDS


def is_retryable(kind: ErrorKind) -> bool:
    return kind in RETRYABLE_KINDS
