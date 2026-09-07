"""Every connector's tools, merged into what the model can call.

A connector tool is indistinguishable from any other capability once it gets
here, which is the point: the same permission decision, the same risk check, the
same executor. What differs is where its risk comes from — a built-in declares
its own, and a connector tool's has to be inferred, because its name and
description come from a server this build has never seen.

**Two separate things, deliberately not merged.** A standing PERMISSION answers
"is Jarvis allowed to use this at all" and is the user's call per tool. A runtime
CONFIRMATION answers "does using an allowed tool pause to ask right now", and
comes from the risk classifier. Neither can suppress the other: setting a tool to
"always allow" does not make a risky one stop confirming.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..capabilities import CapabilityKind, CapabilityRegistry, CapabilitySpec, Risk
from . import api_client, cli_client, files_connector, mcp_client, risk, store

logger = logging.getLogger(__name__)

NAME_SEPARATOR = "__"
_UNSAFE = re.compile(r"[^a-z0-9]+")

#: Two connectors can each offer a `search`, so a user-added connector's tools
#: are prefixed with its label. The files connector is a singleton and cannot
#: collide, so its names stay plain — `read_file` is what it should be called.
UNPREFIXED_TYPES = ("files",)


def _identifier(text: str) -> str:
    return _UNSAFE.sub("_", str(text or "").lower()).strip("_")[:40] or "connector"


def prefixed_name(connector: dict[str, Any], tool_name: str) -> str:
    if connector.get("type") in UNPREFIXED_TYPES:
        return tool_name
    return (f"{_identifier(connector.get('label') or connector.get('id'))}"
            f"{NAME_SEPARATOR}{_identifier(tool_name)}")


def _raw_tools(connector: dict[str, Any]) -> list[dict[str, Any]]:
    config = connector.get("config") or {}
    kind = connector.get("type")
    if kind == "files":
        return list(files_connector.TOOLS)
    if kind == "api":
        return api_client.tool_declarations(config)
    if kind == "cli":
        return cli_client.tool_declarations(config)
    if kind == "mcp":
        return mcp_client.tool_declarations(config)
    return []


def _dispatch(connector_id: str, kind: str, tool_name: str,
              args: dict[str, Any]) -> dict[str, Any]:
    """Re-read the connector at call time.

    Not closed over: its allowlist or its key may have changed since the
    declaration was built, and acting on a stale copy of a permission is the
    kind of mistake that only shows up once it matters.
    """
    connector = store.get_connector(connector_id)
    if connector is None:
        return {"ok": False, "error": "That connection has been removed."}
    if not connector.get("enabled", True):
        return {"ok": False, "error": f'"{connector["label"]}" is switched off.'}
    if store.tool_permission(connector, tool_name) == "deny":
        return {"ok": False,
                "error": f'The user has turned "{tool_name}" off for this connection.'}

    config = connector.get("config") or {}
    try:
        if kind == "files":
            return files_connector.dispatch(tool_name, args, config)
        if kind == "api":
            return api_client.dispatch(tool_name, args, config)
        if kind == "cli":
            return cli_client.dispatch(tool_name, args, config)
        if kind == "mcp":
            return mcp_client.dispatch(tool_name, args, config)
    except PermissionError as err:
        return {"ok": False, "error": str(err)}
    except (ValueError, KeyError, FileNotFoundError, NotADirectoryError) as err:
        return {"ok": False, "error": str(err)}
    except Exception as err:  # noqa: BLE001 — an external service failing is a result
        logger.info("connector %s failed on %s: %s", connector_id, tool_name, err)
        return {"ok": False, "error": f"{tool_name} didn't work: {err}"}
    return {"ok": False, "error": f"{tool_name} is not something that connection can do."}


def connector_specs(connector: dict[str, Any]) -> list[CapabilitySpec]:
    specs: list[CapabilitySpec] = []
    kind = str(connector.get("type"))
    for tool in _raw_tools(connector):
        name = prefixed_name(connector, tool["name"])
        permission = store.tool_permission(connector, tool["name"])
        if permission == "deny":
            # Not declared at all: a tool the user has turned off should not be
            # something the model has to be refused, it should be absent.
            continue

        classified = risk.classify(tool["name"], tool.get("description", ""))
        specs.append(CapabilitySpec(
            id=f"connector.{connector['id']}.{tool['name']}",
            name=name,
            description=tool.get("description") or name,
            input_schema=tool.get("parameters") or {"type": "object", "properties": {}},
            # An inferred "risky" and a user's own "ask" both mean confirm, and
            # neither can override the other into not asking.
            risk=Risk.MEDIUM if (classified == "risky" or permission == "ask") else Risk.LOW,
            handler=(lambda _cid=connector["id"], _kind=kind, _tool=tool["name"], **args:
                     _dispatch(_cid, _kind, _tool, args)),
            kind=CapabilityKind.CONNECTOR,
            timeout_s=120.0 if kind == "mcp" else 60.0,
            tags=frozenset({"core"}) if kind == "files" else frozenset(),
            summarize=(lambda args, _label=connector.get("label"), _tool=tool["name"]:
                       f'Use "{_tool}" on {_label}?'),
        ))
    return specs


def sync(registry: CapabilityRegistry) -> list[str]:
    """Register every enabled connector's tools and drop what is gone."""
    wanted: list[str] = []
    for connector in store.list_connectors():
        if not connector.get("enabled", True):
            continue
        for spec in connector_specs(connector):
            try:
                registry.register(spec)
                wanted.append(spec.name)
            except Exception:  # noqa: BLE001 — a name collision must not hide the rest
                logger.warning("could not register %s from %s", spec.name,
                               connector.get("label"))

    for spec in registry.list(kind=CapabilityKind.CONNECTOR):
        if spec.name not in wanted:
            registry.unregister(spec.name)
    return sorted(wanted)


def refresh_tools(connector_id: str) -> dict[str, Any]:
    """Ask a connector what it can do now, and remember the answer.

    The only place that reaches out: everything else reads what was found here,
    so building a declaration list never starts a process or makes a request.
    """
    connector = store.get_connector(connector_id)
    if connector is None:
        raise KeyError("That connector no longer exists.")
    config = dict(connector.get("config") or {})

    if connector["type"] == "mcp":
        config["tools"] = mcp_client.fetch_tools(config)
        found = len(config["tools"])
    elif connector["type"] == "api" and config.get("specUrl"):
        discovered = api_client.discover_from_spec(config["specUrl"])
        config["baseUrl"] = discovered["baseUrl"] or config.get("baseUrl")
        config["operations"] = discovered["operations"]
        found = len(config["operations"])
    else:
        found = len(_raw_tools(connector))

    updated = store.update_connector(connector_id, {
        "config": config,
        "status": {"state": "working", "checkedAt": None, "detail": f"{found} tools"}})
    return {"ok": True, "connector": updated, "tools": found}
