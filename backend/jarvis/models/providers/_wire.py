"""What every provider needs and none should write twice: making the request,
reading a stream of server-sent events, and turning a failure into words.

Not an adapter framework — three small functions and a few helpers. Anything that
is about ONE provider's format lives in that provider's own module.
"""

from __future__ import annotations

import json
import ssl
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from ...conversation import assistant_text_of
from ...prompt_format import CACHE_BREAK
from ...redact import redact_text
from ..errors import ProviderError

CONNECT_TIMEOUT_S = 10.0
CHECK_TIMEOUT_S = 20.0
#: A large local model can take a long while to say its first word. Waiting is
#: normal there, so this is generous; the connect timeout is what catches a
#: server that simply isn't running.
STREAM_READ_TIMEOUT_S = 180.0
USER_AGENT = "Jarvis/1.0 (personal assistant)"

assistant_text = assistant_text_of


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


def is_error_result(result: Any) -> bool:
    """A tool result the executor produced as a failure (`{"error": ...}`)."""
    return isinstance(result, dict) and bool(result.get("error"))


def media_of(message: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(kind, mimeType, base64)` for each attachment whose bytes are on hand."""
    found = []
    for item in message.get("media") or []:
        data = item.get("dataBase64")
        if data:
            found.append((str(item.get("kind") or "image"), str(item.get("mimeType") or ""), data))
    return found


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
            kind="reply",
        )
    return value


# --- failure, in words -----------------------------------------------------------

def _provider_words(response: httpx.Response) -> str:
    """What the provider itself said, as short plain text."""
    try:
        body = response.json()
    except ValueError:
        body = None
    message: Any = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message")
        elif isinstance(err, str):
            message = err
        message = message or body.get("message") or body.get("detail")
    if not message:
        message = response.text
    text = " ".join(str(message).split())
    # A provider's own error can echo the request — including the key that was sent.
    return redact_text(text[:400]) or ""


def error_for(status: int, words: str, url: str, *, missing: str = "model") -> ProviderError:
    """`missing` says what a 404 means here: 'model' (the thing asked for isn't
    there) or 'address' (nothing here speaks this kind of API)."""
    tail = f" {words}" if words else ""
    if status == 401:
        return ProviderError(f"The provider didn't accept the key (401).{tail}", kind="auth", status=status)
    if status == 403:
        return ProviderError(f"The provider refused this request (403).{tail}", kind="forbidden", status=status)
    if status == 402:
        return ProviderError(f"The provider says this needs credit on the account (402).{tail}",
                             kind="billing", status=status)
    if status == 404:
        if missing == "address":
            return ProviderError(
                f"Nothing at {host_of(url)} answered like a model provider (404). "
                f"Check the address.{tail}", kind="request", status=status)
        return ProviderError(f"The provider says it can't find that (404).{tail}", kind="model", status=status)
    if status == 429:
        return ProviderError(f"The provider is limiting requests right now (429).{tail}", kind="rate", status=status)
    if status >= 500:
        return ProviderError(f"The provider had a problem on its end ({status}).{tail}", kind="server", status=status)
    if 300 <= status < 400:
        return ProviderError(
            f"{host_of(url)} redirected the request elsewhere ({status}), which Jarvis won't follow "
            "with a key attached. Use the address it redirects to.", kind="request", status=status)
    return ProviderError(f"The provider couldn't use that request ({status}).{tail}", kind="request", status=status)


def network_error(err: Exception, url: str) -> ProviderError:
    host = host_of(url)
    if isinstance(err, (httpx.ConnectError, httpx.ConnectTimeout)):
        return ProviderError(
            f"Couldn't reach {host}. If it's a program on this computer, check that it's "
            "running and that the address is right.", kind="network")
    if isinstance(err, httpx.TimeoutException):
        return ProviderError(f"{host} took too long to answer.", kind="network")
    return ProviderError(f"The connection to {host} broke: {redact_text(str(err))}", kind="network")


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


def get_json(url: str, *, headers: dict[str, str] | None = None,
             params: dict[str, Any] | None = None, missing: str = "model",
             timeout: float = CHECK_TIMEOUT_S) -> Any:
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
        raise error_for(response.status_code, _provider_words(response), url, missing=missing)
    try:
        return response.json()
    except ValueError as err:
        raise ProviderError(
            f"{host_of(url)} answered, but not with the kind of reply a model provider gives.",
            kind="request") from err


def probe_post(url: str, *, headers: dict[str, str] | None = None,
               body: dict[str, Any] | None = None) -> tuple[int, str]:
    """POST something deliberately incomplete and report only how the far end
    answered: `(status, the provider's words)`. Never raises for a refusal.

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
    return response.status_code, _provider_words(response)


@contextmanager
def post_stream(url: str, *, headers: dict[str, str] | None = None,
                body: dict[str, Any], read_timeout: float | None = None) -> Iterator[httpx.Response]:
    """Open a streaming POST. A refusal is raised as a `ProviderError` before any
    of the body is handed over; a connection that breaks part-way is raised as one
    too, from wherever the caller was reading."""
    timeout = httpx.Timeout(read_timeout or STREAM_READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    try:
        with httpx.Client(timeout=timeout, verify=_verify()) as client:
            with client.stream("POST", url, json=body,
                               headers=_headers({"Content-Type": "application/json", **(headers or {})})) as response:
                if response.status_code >= 300:
                    response.read()
                    raise error_for(response.status_code, _provider_words(response), url)
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
                            kind="reply") from err
