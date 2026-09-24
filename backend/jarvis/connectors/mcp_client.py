"""An MCP server as a set of callable tools, over stdio or HTTP.

**Uses the official MCP client library**, which is already a declared dependency
of this project. The original hand-wrote JSON-RPC over a spawned process's
stdin and stdout specifically to avoid adding one — a real decision in a runtime
where the library was a new dependency, and simply not the situation here. The
protocol is now the library's problem rather than something this build can get
subtly wrong.

**Connecting happens per call, not once and kept.** A held-open subprocess is a
lifecycle to get right — reaping it, noticing it died, not leaking one per
connector across a long run — and this build would rather pay a startup per call
than be the thing holding a stale process. The tool LIST is cached in the
connector's own config, refreshed explicitly, so a declaration list never starts
a server at all. That is a real trade: a call is slower than it would be against
a warm process. Stated rather than hidden.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 120.0
MAX_RESULT_CHARS = 20000


def _connect_target(config: dict[str, Any]) -> Any:
    """What to hand the client: a URL for a remote server, or launch parameters
    for a local one."""
    from mcp import StdioServerParameters

    flow = (config or {}).get("connectFlow") or {}
    kind = flow.get("kind")
    if kind == "stdio":
        if not flow.get("command"):
            raise ValueError("That MCP connector has no command to run.")
        return StdioServerParameters(command=flow["command"], args=flow.get("args") or [],
                                     env=flow.get("env") or None)
    if flow.get("url"):
        return str(flow["url"])
    raise ValueError("That MCP connector has no server address or command.")


async def _resolve_token(connector_id: str | None, config: dict[str, Any]) -> str | None:
    """The bearer token to send, resolved the right way for how this
    connector was actually set up.

    A connector `oauth.py` connected holds a whole TOKEN SET under
    `secretRef` (access token, refresh token, expiry — see `oauth.py`'s
    `_token_set_from()`), never a bare bearer string. Reading it as a plain
    secret and sending the raw JSON as `Authorization: Bearer <json blob>` is
    broken on the very first call and silently never refreshes an expiring
    token. `oauth.get_access_token()` is what actually understands that
    shape (and refreshes first if due) — used whenever this connector's
    `connectFlow.kind` says it went through that flow. Anything else (a
    plain pasted API token, `kind: 'none'`/no OAuth at all) still reads
    `secretRef` directly, exactly as before.
    """
    from . import oauth as oauth_module

    if connector_id and oauth_module.is_oauth_flow(config):
        try:
            return await oauth_module.get_access_token(connector_id)
        except RuntimeError:
            return None

    from ..config import get_secret

    secret_ref = (config or {}).get("secretRef")
    return get_secret(secret_ref) if secret_ref else None


async def _with_client(config: dict[str, Any], work: Any, *, connector_id: str | None = None) -> Any:
    from mcp import Client

    headers = {}
    token = await _resolve_token(connector_id, config)
    if token:
        # Only ever sent to the server this connector names, and never returned
        # anywhere a model can see it.
        headers["Authorization"] = f"Bearer {token}"

    target = _connect_target(config)
    if not isinstance(target, str):
        try:
            async with Client(target, read_timeout_seconds=CALL_TIMEOUT_S) as client:
                return await work(client)
        except Exception as err:  # noqa: BLE001 — reworded, never swallowed
            raise RuntimeError(_plain_failure(err, [], None)) from err

    # A bare URL handed to `Client` gets a transport with no way to carry a
    # header, so the token above was built and then silently never sent — every
    # server that needs signing in answered 401. The library's own HTTP client,
    # passed to its own transport, is how a header reaches the wire.
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    # The library reports a refused request as a bare "Server returned an error
    # response", with the status gone; this is the only place it is still seen.
    statuses: list[int] = []

    async def _note_status(response: Any) -> None:
        statuses.append(response.status_code)

    try:
        async with create_mcp_http_client(headers=headers) as http:
            http.event_hooks["response"].append(_note_status)
            transport = streamable_http_client(target, http_client=http)
            async with Client(transport, read_timeout_seconds=CALL_TIMEOUT_S) as client:
                return await work(client)
    except Exception as err:  # noqa: BLE001 — reworded, never swallowed
        raise RuntimeError(_plain_failure(err, statuses, target)) from err


def _innermost(err: BaseException) -> BaseException:
    while isinstance(err, BaseExceptionGroup) and err.exceptions:
        err = err.exceptions[0]
    return err


def _plain_failure(err: BaseException, statuses: list[int], url: str | None) -> str:
    """What went wrong, in words the person can act on.

    The client library wraps every failure in task-group exceptions, so what
    reached the screen was "unhandled errors in a TaskGroup (1 sub-exception)"
    — true, and no use to anyone.
    """
    if any(code in (401, 403) for code in statuses):
        return ("The app didn't accept Jarvis's sign-in. Disconnect it and connect again.")
    leaf = _innermost(err)
    name = type(leaf).__name__
    if url is None and isinstance(leaf, OSError):
        return f"Couldn't start that app's program on this computer: {leaf}"
    if name in ("ConnectError", "ConnectTimeout") or isinstance(leaf, (ConnectionError, OSError)):
        return f"Couldn't reach the app's server at {url}. Check it is running and try again."
    if name in ("ReadTimeout", "TimeoutError") or isinstance(leaf, TimeoutError):
        return "The app's server took too long to answer."
    if any(code >= 500 for code in statuses):
        return "The app's server had a problem answering. Try again in a moment."
    return str(leaf) or name


def _run(coroutine: Any) -> Any:
    """Drive an async client from sync code.

    Safe here because capability handlers run on a worker thread with no event
    loop of its own — but checked rather than assumed, because "there is no loop
    running" is exactly the kind of thing that stops being true later.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise RuntimeError("an MCP call cannot run inside an existing event loop")


def _tool_shape(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    return {"name": tool.name,
            "description": getattr(tool, "description", "") or tool.name,
            "parameters": schema or {"type": "object", "properties": {}}}


def fetch_tools(config: dict[str, Any], *, connector_id: str | None = None) -> list[dict[str, Any]]:
    """Ask the server what it offers. Reaches the network or spawns a process,
    so it is called when refreshing a connector, never per turn."""
    async def work(client: Any) -> Any:
        listed = await client.list_tools()
        return [_tool_shape(tool) for tool in listed.tools]

    return _run(_with_client(config, work, connector_id=connector_id))


def tool_declarations(config: dict[str, Any]) -> list[dict[str, Any]]:
    """What was found last time this connector was refreshed. A pure read: no
    process is started to build a declaration list."""
    return list((config or {}).get("tools") or [])


def _flatten(content: Any) -> str:
    parts = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            parts.append(str(getattr(block, "type", "content")))
    return "\n".join(parts)


def dispatch(name: str, args: dict[str, Any], config: dict[str, Any],
            *, connector_id: str | None = None) -> dict[str, Any]:
    async def work(client: Any) -> Any:
        return await client.call_tool(name, args or {})

    result = _run(_with_client(config, work, connector_id=connector_id))
    text = _flatten(getattr(result, "content", None))
    failed = bool(getattr(result, "isError", False))
    answer: dict[str, Any] = {"ok": not failed,
                              "truncated": len(text) > MAX_RESULT_CHARS,
                              "result": text[:MAX_RESULT_CHARS]}
    if failed:
        answer["error"] = text[:1000] or f"{name} reported a failure with no detail."
    return answer
