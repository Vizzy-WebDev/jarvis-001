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

Nothing legitimate carries them. The app itself is same-origin; a typed or
bookmarked URL is `Sec-Fetch-Site: none`; curl, an agent calling the local API,
and the test suite send neither header.
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


class RequestGuard:
    """ASGI middleware — wraps the whole app, so it runs before any route."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope.get("type") in ("http", "websocket")
                and str(scope.get("path") or "").startswith("/api")
                and is_foreign(scope.get("headers") or [])):
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
