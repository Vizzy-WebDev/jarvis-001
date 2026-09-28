"""What every provider needs and none should write twice: making the request,
reading a stream of server-sent events, and turning a failure into words.

Not an adapter framework — a few small functions and helpers. Anything that is about
ONE provider's format lives in that provider's own module — including what its error
bodies MEAN: each module's `normalize_error` reads its own provider's body and decides
the scope, and only falls back to `fallback` here (the HTTP status alone) when the
provider's body said nothing it recognises.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlparse

import httpx

from ...prompt_format import CACHE_BREAK
from ...redact import redact_text
from ..errors import ProviderError
from ..request import ChatRequest
from ..types import RawError

CONNECT_TIMEOUT_S = 10.0
CHECK_TIMEOUT_S = 20.0
#: How long a streamed reply may go with NO bytes at all before it is given up on.
#: This is an INACTIVITY limit — it restarts on every chunk that arrives, pings
#: included — never a total-time limit, and it is deliberately huge: a reasoning model
#: can think silently for minutes, and a slow first word is not a failure. It exists
#: only so a connection that has truly gone dead cannot hang forever. TCP keep-alive
#: (below) notices a dead connection far sooner, without touching a model that is
#: still working. The connect timeout is what catches a server that isn't running.
SILENCE_CEILING_S = 600.0
USER_AGENT = "Jarvis/1.0 (personal assistant)"

#: A provider module's own reading of its failures (`normalize_error`).
Normalize = Callable[[RawError], ProviderError]


def flatten_system(system: str | None) -> str:
    """One string, for every provider that takes a single system prompt.

    The cache marker is a signal to Anthropic's provider alone — it splits on it
    to place a cache breakpoint. Anywhere else it would be read as literal text.
    """
    return (system or "").replace(CACHE_BREAK, "\n\n")


def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def host_of(url: str) -> str:
    return urlparse(url).netloc or url


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def count(value: Any) -> int | None:
    """A token count the provider reported — `None` for anything that isn't one."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def reasoning_level(request: ChatRequest, facts: Mapping[str, Any] | None) -> str | None:
    """The neutral reasoning level to send, or None.

    Only a level the model's provider REPORTED it accepts is ever sent: a request that
    asks for reasoning a model was never said to support is sent without it — dropped
    silently, never refused and never guessed at.
    """
    level = request.options.reasoning
    reported = (facts or {}).get("reasoning")
    if not level or not isinstance(reported, dict) or reported.get("supported") is not True:
        return None
    levels = reported.get("levels")
    if isinstance(levels, list) and levels and level not in levels:
        return None
    return level


def parse_arguments(text: str | None, tool: str) -> dict[str, Any]:
    """A tool call's arguments, which arrive as text.

    Empty means none. Anything else that is not a JSON object is refused rather
    than guessed at: a tool call misread and then RUN is worse than a turn that
    says it could not read the request.
    """
    if not text or not text.strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise ProviderError(
            f"The model asked to use “{tool}”, but its request was garbled, so nothing was run.",
            kind="reply", scope="model",
        )
    return value


# --- failure, in words -----------------------------------------------------------

def words_of(body: Any, text: str = "") -> str:
    """What the provider itself said, as short plain text."""
    message: Any = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message")
        elif isinstance(err, str):
            message = err
        message = message or body.get("message") or body.get("detail")
    if not message:
        message = text
    flat = " ".join(str(message).split())
    # A provider's own error can echo the request — including the key that was sent.
    return redact_text(flat[:400]) or ""


def raw_error(response: httpx.Response, url: str) -> RawError:
    """A failed response as it arrived: status, headers, parsed body, and its words."""
    try:
        body = response.json()
    except ValueError:
        body = None
    return RawError(status=response.status_code, headers={k.lower(): v for k, v in response.headers.items()},
                    body=body, words=words_of(body, response.text), url=url)


def in_band(body: Any, url: str, status: int | None = None) -> RawError:
    """A failure reported INSIDE a stream that had already answered 200."""
    return RawError(status=status, headers={}, body=body, words=words_of(body), url=url)


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def duration_s(text: Any) -> float | None:
    """A duration a provider wrote down: "30", "1.5s", "6m0s", "20ms", "1h2m"."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text) if text >= 0 else None
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    parts = _DURATION.findall(value)
    if not parts or "".join(n + u for n, u in parts) != value.replace(" ", ""):
        return None
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(n) * scale[u] for n, u in parts)


def retry_after_s(headers: Mapping[str, str], *, now: datetime | None = None) -> float | None:
    """The standard wait hint: `retry-after-ms`, or `Retry-After` as seconds or an HTTP date."""
    lower = {k.lower(): v for k, v in headers.items()}
    if "retry-after-ms" in lower:
        ms = duration_s(lower["retry-after-ms"])
        if ms is not None:
            return ms / 1000.0
    value = lower.get("retry-after")
    if not value:
        return None
    seconds = duration_s(value)
    if seconds is not None:
        return seconds
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


def reset_headers_s(headers: Mapping[str, str], prefix: str = "x-ratelimit") -> float | None:
    """`<prefix>-reset-requests` / `<prefix>-reset-tokens`, as OpenAI-style APIs send them.

    When one of the two limits reports nothing remaining, ITS reset is the one that
    matters; otherwise the sooner of the two."""
    lower = {k.lower(): v for k, v in headers.items()}
    found: dict[str, float] = {}
    for which in ("requests", "tokens"):
        value = duration_s(lower.get(f"{prefix}-reset-{which}"))
        if value is not None:
            found[which] = value
    if not found:
        return None
    exhausted = [found[w] for w in found if str(lower.get(f"{prefix}-remaining-{w}", "")).strip() == "0"]
    return max(exhausted) if exhausted else min(found.values())


_SAID = {
    "auth": "The provider didn't accept the key",
    "forbidden": "The provider refused this request",
    "billing": "The provider says this needs credit on the account",
    "model": "The provider says it can't find that",
    "rate": "The provider is limiting requests right now",
    "overloaded": "The provider is overloaded right now",
    "server": "The provider had a problem on its end",
    "request": "The provider couldn't use that request",
}


def make(raw: RawError, *, kind: str, scope: str, retryable: bool = False,
         retry_after_s: float | None = None, message: str | None = None) -> ProviderError:
    """A `ProviderError` from a provider module's own verdict, worded the usual way."""
    if message is None:
        tail = f" {raw.words}" if raw.words else ""
        code = f" ({raw.status})" if raw.status is not None else ""
        message = f"{_SAID.get(kind, 'The provider failed')}{code}.{tail}"
    return ProviderError(message, kind=kind, scope=scope, retryable_elsewhere=retryable,
                         retry_after_s=retry_after_s, status=raw.status)


def fallback(raw: RawError, *, missing: str = "model") -> ProviderError:
    """What the HTTP status alone says, for a body the provider module didn't recognise.

    `missing` says what a 404 means here: 'model' (the thing asked for isn't there)
    or 'address' (nothing here speaks this kind of API)."""
    status, url, retry = raw.status, raw.url, retry_after_s(raw.headers)
    tail = f" {raw.words}" if raw.words else ""
    if status is None:
        return make(raw, kind="server", scope="unknown",
                    message=f"{host_of(url)} stopped the reply: {raw.words or 'it reported an error.'}")
    if status == 401:
        return make(raw, kind="auth", scope="credential")
    if status == 403:
        return make(raw, kind="forbidden", scope="model")
    if status == 402:
        return make(raw, kind="billing", scope="credential", retry_after_s=retry)
    if status == 404:
        if missing == "address":
            return make(raw, kind="request", scope="provider",
                        message=f"Nothing at {host_of(url)} answered like a model provider (404). "
                                f"Check the address.{tail}")
        return make(raw, kind="model", scope="model")
    if status == 429:
        return make(raw, kind="rate", scope="model", retryable=True, retry_after_s=retry)
    if status >= 500:
        return make(raw, kind="server", scope="provider", retryable=True, retry_after_s=retry)
    if 300 <= status < 400:
        return make(raw, kind="request", scope="provider",
                    message=f"{host_of(url)} redirected the request elsewhere ({status}), which Jarvis won't "
                            "follow with a key attached. Use the address it redirects to.")
    return make(raw, kind="request", scope="request")


def error_for(status: int, words: str, url: str, *, missing: str = "model") -> ProviderError:
    """The status-only reading, for a caller holding nothing but a status and words."""
    return fallback(RawError(status=status, words=words, url=url), missing=missing)


def network_error(err: Exception, url: str) -> ProviderError:
    host = host_of(url)
    if isinstance(err, (httpx.ConnectError, httpx.ConnectTimeout)):
        return ProviderError(
            f"Couldn't reach {host}. If it's a program on this computer, check that it's "
            "running and that the address is right.", kind="unreachable", scope="provider")
    if isinstance(err, httpx.TimeoutException):
        return ProviderError(f"{host} took too long to answer.", kind="network", scope="model")
    return ProviderError(f"The connection to {host} broke: {redact_text(str(err))}", kind="network",
                         scope="model")


# --- requests --------------------------------------------------------------------

_ssl_context: ssl.SSLContext | None = None


def _verify() -> ssl.SSLContext:
    """One TLS context for every request, built the first time it is needed.

    A client built without one makes its own — loading the whole certificate
    bundle each time, which measured at over half a second per request on Windows.
    Every model call, connection test and discovery would pay that. A context is
    safe to share across threads and clients; two threads racing to build it just
    build it twice.
    """
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = httpx.create_ssl_context()
    return _ssl_context


def _headers(extra: dict[str, str] | None) -> dict[str, str]:
    return {"User-Agent": USER_AGENT, **(extra or {})}


def _judge(raw: RawError, normalize: Normalize | None, missing: str) -> ProviderError:
    if normalize is not None and not (missing == "address" and raw.status == 404):
        return normalize(raw)
    return fallback(raw, missing=missing)


def get_json(url: str, *, headers: dict[str, str] | None = None,
             params: dict[str, Any] | None = None, missing: str = "model",
             timeout: float = CHECK_TIMEOUT_S, normalize: Normalize | None = None) -> Any:
    """A GET that comes back as JSON, or as a `ProviderError` in plain words.

    Redirects are NOT followed: a custom header carrying a key is not stripped on
    a cross-host redirect the way `Authorization` is, so following one would hand
    the key to whoever the address redirects to.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_S), verify=_verify()) as client:
            response = client.get(url, headers=_headers(headers), params=params)
    except httpx.HTTPError as err:
        raise network_error(err, url) from err
    if response.status_code >= 300:
        raise _judge(raw_error(response, url), normalize, missing)
    try:
        return response.json()
    except ValueError as err:
        raise ProviderError(
            f"{host_of(url)} answered, but not with the kind of reply a model provider gives.",
            kind="request", scope="provider") from err


def probe_post(url: str, *, headers: dict[str, str] | None = None,
               body: dict[str, Any] | None = None) -> RawError:
    """POST something deliberately incomplete and report only how the far end
    answered, as a `RawError` (status, headers, the provider's words). Never raises
    for a refusal.

    A real endpoint answers a malformed request with a complaint about the request
    (400/401/422) while an address that isn't one answers 404 — which is how a
    server with no model list can still be told apart from a mistyped address,
    without spending anything on a real generation.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(CHECK_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                          verify=_verify()) as client:
            response = client.post(url, json=body or {},
                                   headers=_headers({"Content-Type": "application/json", **(headers or {})}))
    except httpx.HTTPError as err:
        raise network_error(err, url) from err
    return raw_error(response, url)


_keepalive_options: list[tuple[int, int, int]] | None = None


def _keepalive() -> list[tuple[int, int, int]]:
    """TCP keep-alive settings: every so often the operating system asks the far end
    whether it is still there. The far end's OS answers even while its model is busy
    thinking, so a reply that is merely slow passes, and a connection that has died
    (network dropped, host gone) fails in about a minute — without any application
    timeout that would also cut off a model that is genuinely reasoning.

    Only options this platform accepts are used: an unsupported one would fail every
    connection, so each is tried once on a throwaway socket first.
    """
    global _keepalive_options
    if _keepalive_options is None:
        wanted = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                wanted.append((socket.IPPROTO_TCP, getattr(socket, name), value))
        usable = []
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            for option in wanted:
                try:
                    probe.setsockopt(*option)
                    usable.append(option)
                except OSError:
                    continue
        finally:
            probe.close()
        _keepalive_options = usable
    return _keepalive_options


def _stream_client(timeout: httpx.Timeout) -> httpx.Client:
    # Setting socket options needs a custom transport, and a custom transport stops httpx
    # reading proxy settings from the environment. Where a proxy is configured, keep the
    # default client (and go without keep-alive) rather than bypass the person's proxy.
    if urllib.request.getproxies():
        return httpx.Client(timeout=timeout, verify=_verify())
    return httpx.Client(timeout=timeout,
                        transport=httpx.HTTPTransport(verify=_verify(), socket_options=_keepalive()))


@contextmanager
def post_stream(url: str, *, headers: dict[str, str] | None = None,
                body: dict[str, Any], normalize: Normalize | None = None) -> Iterator[httpx.Response]:
    """Open a streaming POST. A refusal is raised as a `ProviderError` before any
    of the body is handed over; a connection that breaks part-way is raised as one
    too, from wherever the caller was reading."""
    timeout = httpx.Timeout(SILENCE_CEILING_S, connect=CONNECT_TIMEOUT_S)
    try:
        with _stream_client(timeout) as client:
            with client.stream("POST", url, json=body,
                               headers=_headers({"Content-Type": "application/json", **(headers or {})})) as response:
                if response.status_code >= 300:
                    response.read()
                    raise _judge(raw_error(response, url), normalize, "model")
                yield response
    except httpx.HTTPError as err:
        raise network_error(err, url) from err


def iter_sse(response: httpx.Response) -> Iterator[tuple[str | None, str]]:
    """`(event, data)` for each server-sent event. Comment lines are skipped and a
    multi-line `data:` is joined, per the format."""
    event: str | None = None
    data: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


def loads_event(data: str, provider: str) -> Any:
    """One event's JSON. A chunk that is not JSON means the stream is not what it
    claimed to be — said plainly, not swallowed."""
    try:
        return json.loads(data)
    except ValueError as err:
        raise ProviderError(f"{provider} sent part of its reply in a form Jarvis couldn't read.",
                            kind="reply", scope="model") from err
