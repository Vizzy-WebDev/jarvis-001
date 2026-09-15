"""Answering and acting on the bell, from plain conversation.

Before this file, Jarvis had no way to act on its own notifications when
asked — only `open_section.py`'s `navigate` to the screen. Four tools here,
two LOW (read-only or merely marks something seen) and two that touch the
recycle bin: MEDIUM for moving things there (reversible — matches the Risk
enum's own "move files" example) and HIGH for emptying it (genuinely
irreversible — "delete files"). The confirmation on the last two comes from
the risk classification itself, not a per-tool flag, the same as every other
confirm-gated tool in this package.

"Clear" and "delete" are treated as synonyms pointing at the same, reversible
action deliberately: a spoken "delete my notifications" is ambiguous about
whether the user means the recycle bin exists, and defaulting to the
recoverable interpretation is the safer reading. Only a request that
specifically names the recycle bin/trash, or says "permanently", reaches the
irreversible tool.
"""

from __future__ import annotations

from typing import Any

from .. import notifications
from ..capabilities import CapabilitySpec, Risk


def _status(**_: Any) -> dict[str, Any]:
    active = notifications.listed()
    unread = sum(1 for row in active if not row.get("read"))
    return {
        "ok": True,
        "total": len(active),
        "unread": unread,
        "read": len(active) - unread,
        "inRecycleBin": len(notifications.trash_listed()),
    }


def _mark_all_read(**_: Any) -> dict[str, Any]:
    notifications.mark_all_read()
    return {"ok": True, "note": "Marked everything as read."}


def _clear(**_: Any) -> dict[str, Any]:
    moved = len(notifications.listed())
    notifications.clear_all()
    return {"ok": True, "moved": moved,
            "note": f"Moved {moved} notification(s) to the recycle bin. "
                    "They'll be gone for good after 30 days, or sooner if you ask."}


def _clear_summary(_args: dict[str, Any]) -> str:
    count = len(notifications.listed())
    if count == 0:
        return "There's nothing to clear."
    return f"Move all {count} notification(s) to the recycle bin?"


def _empty_bin(**_: Any) -> dict[str, Any]:
    removed = notifications.empty_trash()
    return {"ok": True, "removed": removed,
            "note": f"Permanently deleted {removed} notification(s) from the recycle bin."}


def _empty_bin_summary(_args: dict[str, Any]) -> str:
    count = len(notifications.trash_listed())
    if count == 0:
        return "The recycle bin is already empty."
    return (f"Permanently delete {count} notification(s) from the recycle bin? "
            "This cannot be undone.")


SPECS = [
    CapabilitySpec(
        id="builtin.notifications_status", name="notifications_status",
        description=('How many notifications there are, and how many are read/unread. Use '
                     'for "how many notifications do I have", "how many are unread", and '
                     'similar questions.'),
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.LOW, handler=_status, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.mark_notifications_read", name="mark_notifications_read",
        description=('Mark every notification as read. Use for "read them for me", "mark my '
                     'notifications as read", "mark all as read", and similar.'),
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.LOW, handler=_mark_all_read, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.clear_notifications", name="clear_notifications",
        description=('Move every current notification to the recycle bin. Reversible, NOT a '
                     'permanent delete — items can still be restored or take up to 30 days to '
                     'disappear for good. Use for "clear my notifications", "clear them", '
                     '"delete my notifications", "delete them" — a plain request to clear or '
                     'delete notifications defaults to this safe, recoverable action, never '
                     'the permanent one.'),
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.MEDIUM, handler=_clear, summarize=_clear_summary,
        timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.empty_notifications_recycle_bin", name="empty_notifications_recycle_bin",
        description=('Permanently and irreversibly delete everything in the notifications '
                     'recycle bin. Use ONLY when the user specifically asks to "empty the '
                     'recycle bin", "empty the trash", or "permanently delete" their '
                     'notifications — never for a plain "clear" or "delete" request, which '
                     'means clear_notifications instead.'),
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.HIGH, handler=_empty_bin, summarize=_empty_bin_summary,
        timeout_s=10.0, tags=frozenset({"meta"}),
    ),
]
