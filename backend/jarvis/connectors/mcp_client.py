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


async def _with_client(config: dict[str, Any], work: Any) -> Any:
    from mcp import Client

    headers = {}
    from ..config import get_secret

    secret_ref = (config or {}).get("secretRef")
    token = get_secret(secret_ref) if secret_ref else None
    if token:
        # Only ever sent to the server this connector names, and never returned
        # anywhere a model can see it.
        headers["Authorization"] = f"Bearer {token}"

    target = _connect_target(config)
    client = Client(target, read_timeout_seconds=CALL_TIMEOUT_S)
    async with client:
        return await work(client)


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


def fetch_tools(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Ask the server what it offers. Reaches the network or spawns a process,
    so it is called when refreshing a connector, never per turn."""
    async def work(client: Any) -> Any:
        listed = await client.list_tools()
        return [_tool_shape(tool) for tool in listed.tools]

    return _run(_with_client(config, work))


def tool_declarations(config: dict[str, Any]) -> list[dict[str, Any]]:
    """What was found last time this connector was refreshed. A pure read: no
    process is started to build a declaration list."""
    return [t for t in (config or {}).get("tools") or [] if t.get("enabled", True)]


def _flatten(content: Any) -> str:
    parts = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            parts.append(str(getattr(block, "type", "content")))
    return "\n".join(parts)


def dispatch(name: str, args: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    async def work(client: Any) -> Any:
        return await client.call_tool(name, args or {})

    result = _run(_with_client(config, work))
    text = _flatten(getattr(result, "content", None))
    failed = bool(getattr(result, "isError", False))
    answer: dict[str, Any] = {"ok": not failed,
                              "truncated": len(text) > MAX_RESULT_CHARS,
                              "result": text[:MAX_RESULT_CHARS]}
    if failed:
        answer["error"] = text[:1000] or f"{name} reported a failure with no detail."
    return answer
