"""Refuse API calls a browser marks as coming from somewhere that is not this app.

Why this exists: the Artifacts viewer runs a model-written web page (an `.html`
artifact, a small interactive tool) inside the app, in a sandboxed frame with no
`allow-same-origin`. That frame has an opaque origin and a strict content
policy, so it cannot read anything from Jarvis — but a page can still make its
own frame NAVIGATE to a URL, and some of Jarvis's API does real work on a plain
GET (`/api/chat/stream` runs a whole turn). A page must never be able to talk to
Jarvis on the person's behalf.

A browser labels every request with where it came from, and a page cannot forge
those labels: `Origin: null` for anything an opaque origin sends, and
`Sec-Fetch-Site: cross-site` for a request another site started. Either one on an
`/api` request is refused here, for HTTP and WebSocket alike.

Nothing legitimate carries them — with ONE exception. When a person approves
Jarvis on a provider's own page (Canva, ChatCut, ...), the provider redirects their
browser back to `/api/connectors/oauth/callback`, and a browser labels that
navigation `cross-site` by design. `is_oauth_return` lets that one request through:
a GET to that exact path, which the browser itself says is a top-level page
navigation (`Sec-Fetch-Dest: document`), never an opaque origin. It is safe because
the route redeems nothing without the one-time, unguessable `state` Jarvis made when
the person clicked Connect. Everything else stays refused.

The app itself is same-origin; a typed or bookmarked URL is `Sec-Fetch-Site: none`;
curl, an agent calling the local API, and the test suite send neither header.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

REFUSAL = ("That request came from a page outside Jarvis (for example a web page "
           "Jarvis made, running in its viewer), so it was refused.")


def is_foreign(headers: list[tuple[bytes, bytes]]) -> bool:
    found = {name.lower(): value for name, value in headers}
    origin = found.get(b"origin", b"").strip().lower()
    site = found.get(b"sec-fetch-site", b"").strip().lower()
    return origin == b"null" or site == b"cross-site"


OAUTH_CALLBACK_PATH = "/api/connectors/oauth/callback"


def is_oauth_return(scope: Scope) -> bool:
    """A provider's redirect back to Jarvis after the person approved it."""
    if scope.get("type") != "http" or scope.get("method") != "GET":
        return False
    if str(scope.get("path") or "").rstrip("/") != OAUTH_CALLBACK_PATH:
        return False
    found = {name.lower(): value.strip().lower() for name, value in scope.get("headers") or []}
    return (found.get(b"origin", b"") != b"null"
            and found.get(b"sec-fetch-mode") == b"navigate"
            and found.get(b"sec-fetch-dest") == b"document")


class RequestGuard:
    """ASGI middleware — wraps the whole app, so it runs before any route."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope.get("type") in ("http", "websocket")
                and str(scope.get("path") or "").startswith("/api")
                and is_foreign(scope.get("headers") or [])
                and not is_oauth_return(scope)):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            body = json.dumps({"ok": False, "error": REFUSAL}).encode()
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
