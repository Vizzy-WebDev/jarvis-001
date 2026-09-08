"""Watching for something, and stopping watching.

Setting a watch is MEDIUM: it commits Jarvis to acting later, when nobody is
looking — the same reason scheduling is. Stopping one is MEDIUM too, because
silently dropping a watch the user is relying on is a loss they only discover by
the thing not happening.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..monitor import engine, store


def _watch(description: str = "", kind: str = "", path: str | None = None,
           url: str | None = None, then: str = "notify") -> dict[str, Any]:
    check: dict[str, Any] = {"kind": kind}
    if path:
        check["path"] = path
    if url:
        check["url"] = url
    try:
        engine.validate(check)
    except engine.UnsupportedCheck as err:
        return {"ok": False, "error": str(err)}
    if kind.startswith("file") and not path:
        return {"ok": False, "error": "I need the path of the file to watch."}
    if kind == "web_page_changed" and not url:
        return {"ok": False, "error": "I need the address of the page to watch."}

    monitor = store.create_monitor(description=description or kind, check=check,
                                   on_trigger={"type": "notify", "text": then})
    return {"ok": True, "id": monitor["id"], "watching": monitor["description"],
            "speak": f"Watching for {monitor['description']}."}


def _stop(which: str = "") -> dict[str, Any]:
    monitor = _find(which)
    if monitor is None:
        return {"ok": False, "error": f'I couldn\'t find a watch matching "{which}".'}
    store.stop_monitor(monitor["id"])
    return {"ok": True, "stopped": monitor["description"]}


def _find(which: str) -> dict[str, Any] | None:
    wanted = (which or "").strip().lower()
    watching = store.list_watching()
    if not wanted:
        return watching[0] if len(watching) == 1 else None
    for monitor in watching:
        if monitor["id"] == which or monitor["description"].lower() == wanted:
            return monitor
    return next((m for m in watching if wanted in m["description"].lower()), None)


SPECS = [
    CapabilitySpec(
        id="builtin.watch_for", name="watch_for",
        description=("Watch for something to happen and say so when it does. Can watch for "
                     "a file appearing or disappearing, a file finishing being written, or "
                     "a web page changing. Cannot yet watch windows, programs or the screen."),
        input_schema={"type": "object", "properties": {
            "description": {"type": "string", "description": "What is being watched for, "
                                                             "in the user's own words."},
            "kind": {"type": "string",
                     "description": '"file_exists", "file_gone", "file_size_stable" or '
                                    '"web_page_changed".'},
            "path": {"type": "string", "description": "For the file kinds."},
            "url": {"type": "string", "description": "For web_page_changed."},
            "then": {"type": "string", "description": "What to say when it happens."}},
            "required": ["description", "kind"]},
        risk=Risk.MEDIUM, handler=_watch,
        summarize=lambda args: f'Watch for {args.get("description")} and tell you when it '
                               "happens?",
        timeout_s=15.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.stop_watching", name="stop_watching",
        description="Stop watching for something.",
        input_schema={"type": "object", "properties": {
            "which": {"type": "string", "description": "Which watch, in their own words."}},
            "required": ["which"]},
        risk=Risk.MEDIUM, handler=_stop,
        summarize=lambda args: f'Stop watching for "{args.get("which")}"?',
        timeout_s=10.0, tags=frozenset({"meta"}),
    ),
]
