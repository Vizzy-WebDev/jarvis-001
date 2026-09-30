"""What every driver needs and none should write twice: HTTP requests over plain
httpx, reading server-sent events, and turning an HTTP failure into a canonical
error. Status codes are read here and in the drivers — nowhere else in the layer.

Not an adapter framework: a few functions. Anything about ONE wire format lives in
that driver's own module.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.request
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

import httpx

from ...redact import redact_text
from .. import errors

CONNECT_TIMEOUT_S = 10.0
LIST_TIMEOUT_S = 20.0
#: How long a streamed reply may go with NO bytes at all before it is given up on.
#: An INACTIVITY limit — it restarts on every chunk — never a total-time limit, and
#: deliberately huge: a reasoning model can think silently for minutes, and a slow
#: first word is not a failure. TCP keep-alive notices a dead connection far sooner.
SILENCE_CEILING_S = 600.0
USER_AGENT = "Jarvis/1.0 (personal assistant)"


def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def host_of(url: str) -> str:
    return urlparse(url).netloc or url


def root_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def tool_output(content: Any) -> str:
    """A tool result as the text a wire format carries: a string stays itself."""
    return content if isinstance(content, str) else dumps(content)


def count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_arguments(text: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(arguments, raw). Empty is no arguments. Anything that isn't a JSON object is
    returned raw, untouched, for the layer to report."""
    if not text or not text.strip():
        return {}, None
    try:
        value = json.loads(text)
    except ValueError:
        return None, text
    return (value, None) if isinstance(value, dict) else (None, text)


# --- failure, in canonical terms ---------------------------------------------------------

_CONTEXT = re.compile(r"context (length|window)|maximum context|too many tokens|prompt is too long|"
                      r"input is too long|exceeds? the (maximum|context)|reduce the length|token limit", re.I)
_BILLING = re.compile(r"insufficient[_ ]quota|exceeded your current quota|billing|credit balance|"
                      r"payment required|out of credits|no credits|insufficient credits", re.I)
_REFUSED = re.compile(r"content (policy|management|filter)|safety|flagged|moderation|prohibited", re.I)


def provider_words(body: Any, text: str = "") -> str:
    """What the provider itself said, as short plain text, with secrets scrubbed."""
    message: Any = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("msg")
        elif isinstance(err, str):
            message = err
        message = message or body.get("message") or body.get("detail")
    if not message:
        message = text
    return redact_text(" ".join(str(message).split())[:400]) or ""


def _retry_after(headers: Mapping[str, str] | None, body: Any) -> float | None:
    value = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if value:
        try:
            return max(float(value), 0.0)
        except ValueError:
            try:
                import time

                return max(parsedate_to_datetime(value).timestamp() - time.time(), 0.0)
            except (TypeError, ValueError):
                pass
    # Some providers put it in the body (e.g. a RetryInfo detail: {"retryDelay": "12s"}).
    found = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', dumps(body)) if body is not None else None
    return float(found.group(1)) if found else None


def error_for(status: int, words: str, url: str, *, headers: Mapping[str, str] | None = None,
              body: Any = None) -> errors.ModelError:
    """One HTTP failure as the canonical error a person can read."""
    tail = f" {words}" if words else ""
    host = host_of(url)
    if status in (401, 403):
        return errors.Auth(f"{host} didn't accept the key ({status}).{tail}")
    if status == 402 or _BILLING.search(words):
        return errors.Auth(f"{host} refused this for billing or quota reasons ({status}).{tail}")
    if status == 429:
        return errors.RateLimited(f"{host} is limiting requests right now (429).{tail}",
                                  retry_after=_retry_after(headers, body))
    if status == 413 or (status in (400, 422) and _CONTEXT.search(words)):
        return errors.ContextTooLong(f"The request was too long for this model ({status}).{tail}")
    if status == 400 and _REFUSED.search(words):
        return errors.ContentRefused(f"{host} refused this request's content ({status}).{tail}")
    if status in (408, 504):
        return errors.Timeout(f"{host} took too long to answer ({status}).{tail}")
    if status >= 500 or status == 409:
        return errors.Unavailable(f"{host} had a problem on its end ({status}).{tail}",
                                  retry_after=_retry_after(headers, body))
    if status == 404:
        return errors.InvalidRequest(f"{host} says it can't find that (404) — check the model id and the "
                                     f"address.{tail}")
    if 300 <= status < 400:
        return errors.InvalidRequest(f"{host} redirected the request elsewhere ({status}), which Jarvis won't "
                                     "follow with a key attached. Use the address it redirects to.")
    return errors.InvalidRequest(f"{host} couldn't use that request ({status}).{tail}")


def error_in_body(body: Any, url: str) -> errors.ModelError:
    """An error reported inside a 200 (a stream frame or a whole reply)."""
    err = body.get("error") if isinstance(body, dict) else None
    words = provider_words(body)
    code = err.get("code") if isinstance(err, dict) else None
    status = code if isinstance(code, int) and not isinstance(code, bool) else None
    if status is None and isinstance(code, str) and code.isdigit():
        status = int(code)
    if status is None and isinstance(err, dict):
        kind = str(err.get("type") or err.get("status") or code or "").lower()
        status = (429 if "rate" in kind or "resource_exhausted" in kind
                  else 401 if "auth" in kind or "permission" in kind
                  else 400 if "invalid" in kind
                  else 503 if "overloaded" in kind or "unavailable" in kind
                  else 500)
    return error_for(status or 500, words, url, body=body)


def network_error(err: Exception, url: str) -> errors.ModelError:
    host = host_of(url)
    if isinstance(err, (httpx.ConnectError, httpx.ConnectTimeout)):
        return errors.Unavailable(f"Couldn't reach {host}. If it's a program on this computer, check that it's "
                                  "running and that the address is right.")
    if isinstance(err, httpx.TimeoutException):
        return errors.Timeout(f"{host} went silent and the reply was given up on.")
    return errors.Unavailable(f"The connection to {host} broke: {redact_text(str(err))}")


# --- requests ------------------------------------------------------------------------------

_ssl_context: ssl.SSLContext | None = None


def _verify() -> ssl.SSLContext:
    """One TLS context for every request: building one per client measured over half
    a second on Windows. Safe to share across threads and clients."""
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = httpx.create_ssl_context()
    return _ssl_context


def _headers(extra: Mapping[str, str] | None) -> dict[str, str]:
    return {"User-Agent": USER_AGENT, **(extra or {})}


def _read_json(response: httpx.Response, url: str) -> Any:
    try:
        return response.json()
    except ValueError as err:
        raise errors.Unavailable(f"{host_of(url)} answered, but not with the kind of reply a model server "
                                 "gives.") from err


def get_json(url: str, *, headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None,
             timeout: float = LIST_TIMEOUT_S) -> Any:
    """A GET answered in JSON, or a canonical error. Redirects are NOT followed: a
    custom header carrying a key isn't stripped on a cross-host redirect."""
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_S), verify=_verify()) as client:
            response = client.get(url, headers=_headers(headers), params=params)
    except httpx.HTTPError as err:
        raise network_error(err, url) from err
    if response.status_code >= 300:
        body = _safe_json(response)
        raise error_for(response.status_code, provider_words(body, response.text), url,
                        headers=response.headers, body=body)
    return _read_json(response, url)


def post_json(url: str, *, headers: Mapping[str, str] | None = None, body: Any,
              timeout: float = SILENCE_CEILING_S) -> Any:
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_S), verify=_verify()) as client:
            response = client.post(url, json=body, headers=_headers({"Content-Type": "application/json",
                                                                     **(headers or {})}))
    except httpx.HTTPError as err:
        raise network_error(err, url) from err
    if response.status_code >= 300:
        parsed = _safe_json(response)
        raise error_for(response.status_code, provider_words(parsed, response.text), url,
                        headers=response.headers, body=parsed)
    return _read_json(response, url)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


_keepalive_options: list[tuple[int, int, int]] | None = None


def _keepalive() -> list[tuple[int, int, int]]:
    """TCP keep-alive: the far end's operating system answers while its model is busy
    thinking, so a reply that is merely slow passes and a dead connection fails in
    about a minute. Only options this platform accepts are used."""
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
    # Socket options need a custom transport, which stops httpx reading proxy settings.
    # Where a proxy is configured, keep the default client rather than bypass it.
    if urllib.request.getproxies():
        return httpx.Client(timeout=timeout, verify=_verify())
    return httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(verify=_verify(), socket_options=_keepalive()))


@contextmanager
def post_stream(url: str, *, headers: Mapping[str, str] | None = None, body: Any) -> Iterator[httpx.Response]:
    """Open a streaming POST. A refusal is raised before any of the body is handed
    over; a connection that breaks part-way is raised from wherever it was read."""
    timeout = httpx.Timeout(SILENCE_CEILING_S, connect=CONNECT_TIMEOUT_S)
    try:
        with _stream_client(timeout) as client:
            with client.stream("POST", url, json=body, headers=_headers({"Content-Type": "application/json",
                                                                         **(headers or {})})) as response:
                if response.status_code >= 300:
                    response.read()
                    parsed = _safe_json(response)
                    raise error_for(response.status_code, provider_words(parsed, response.text), url,
                                    headers=response.headers, body=parsed)
                yield response
    except httpx.HTTPError as err:
        raise network_error(err, url) from err


def iter_sse(response: httpx.Response) -> Iterator[tuple[str | None, str]]:
    """`(event, data)` for each server-sent event. Comments are skipped and a
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


def loads_event(data: str, url: str) -> Any:
    try:
        return json.loads(data)
    except ValueError as err:
        raise errors.Unavailable(f"{host_of(url)} sent part of its reply in a form Jarvis couldn't read.") from err


# --- schemas -------------------------------------------------------------------------------

def inline_refs(schema: Any) -> Any:
    """A schema with its local references (`#/$defs/X`, `#/definitions/X`, or any
    JSON pointer into the schema such as `#/anyOf/0/properties/title`) written out in
    place — the same schema, spelled without `$ref`, for servers that refuse the
    keyword. A reference that loops back on itself, or points outside the schema,
    can't be written out; that raises `Unexpressible`."""
    from ..prepared import Unexpressible

    if not isinstance(schema, dict) or "$ref" not in dumps(schema):
        return schema

    def target(ref: str) -> Any:
        if not ref.startswith("#"):
            raise Unexpressible(f"the schema refers to {ref}, outside itself, which can't be written out here")
        node: Any = schema
        for raw in [p for p in ref[1:].split("/") if p]:
            key = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and key in node:
                node = node[key]
            elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
                node = node[int(key)]
            else:
                raise Unexpressible(f"the schema refers to {ref}, which isn't in it")
        return node

    def resolve(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [resolve(v, seen) for v in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in seen:
                raise Unexpressible(f"the schema's {ref} refers to itself, which can't be written out here")
            written = resolve(target(ref), seen + (ref,))
            rest = {k: resolve(v, seen) for k, v in node.items() if k != "$ref"}
            return {**written, **rest} if isinstance(written, dict) else written
        return {k: resolve(v, seen) for k, v in node.items() if k not in ("$defs", "definitions")}

    return resolve(schema, ())
