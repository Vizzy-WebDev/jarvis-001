"""A real MCP server for tests: the installed `mcp` package's own server,
speaking real Streamable HTTP on a real socket — or stdio, run as a script.

Over HTTP it **refuses any request without the expected bearer token**, the way
a signed-in service does. That is the point of it: the client once built the
token and never sent it, and a stub that accepted anything could not have told.
Every tool call it receives is recorded, with the Authorization header it came
with, so a test can check what actually reached the server rather than what the
client said it did.

    python tests/stub_mcp_server.py stdio      # the same tools over stdio
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from typing import Any

from mcp.server import MCPServer

TOOLS = ("search_notes", "delete_note")


def build_server(calls: list[dict[str, Any]]) -> MCPServer:
    server = MCPServer("stub-notes")

    @server.tool(description="Search the notes for a phrase and list what matches.")
    def search_notes(query: str) -> str:
        calls.append({"tool": "search_notes", "args": {"query": query}})
        return f"Found 1 note about {query}: 'Weekly plan'."

    @server.tool(description="Delete a note permanently.")
    def delete_note(note_id: str) -> str:
        calls.append({"tool": "delete_note", "args": {"note_id": note_id}})
        return f"Deleted {note_id}."

    return server


class _RequireBearer:
    """ASGI wrapper: 401 unless the request carries the expected token."""

    def __init__(self, app: Any, token: str, seen: list[str | None]) -> None:
        self.app, self.token, self.seen = app, token, seen

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization")
        self.seen.append(auth)
        if auth != f"Bearer {self.token}":
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": b"sign in first"})
            return
        await self.app(scope, receive, send)


class StubMcpHttp:
    """Start with `with StubMcpHttp(token) as stub:`; `stub.url` is the endpoint."""

    def __init__(self, token: str = "tok-stub") -> None:
        self.token = token
        self.calls: list[dict[str, Any]] = []
        self.auth_seen: list[str | None] = []
        self.url = ""
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "StubMcpHttp":
        import uvicorn

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        app = _RequireBearer(build_server(self.calls).streamable_http_app(), self.token,
                             self.auth_seen)
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                     log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("stub MCP server did not start")
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}/mcp"
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10)


if __name__ == "__main__" and sys.argv[1:] == ["stdio"]:
    build_server([]).run("stdio")
