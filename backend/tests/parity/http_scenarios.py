"""Scripted HTTP sequences run identically against both implementations.

Fixture replay (tests/test_contract.py) covers READ routes well, but it cannot
cover mutating ones: a recorded `PATCH /api/conversations/<id>` names an id that
only ever existed inside the recording run, so replaying it against a fresh
instance just 404s.

So mutating routes are verified differentially instead. Each scenario below is a
sequence of requests expressed against whatever client it is handed — the real
Node server over HTTP, or the FastAPI app in-process — and returns a transcript
of everything observable. The test passes only when the two transcripts match.

Ids and timestamps are replaced with stable placeholders as they are MINTED, so
the transcript can still assert that a later request used the right id without
depending on what that id was.
"""

from __future__ import annotations

import re
from typing import Any, Callable

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class Transcript:
    """Records each step, substituting placeholders for values minted at runtime."""

    def __init__(self) -> None:
        self.steps: list[Any] = []
        self._aliases: dict[str, str] = {}

    def alias(self, value: str, name: str) -> str:
        """Give a freshly-minted id a stable name for the transcript."""
        self._aliases[value] = f"<{name}>"
        return value

    def scrub(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        if isinstance(value, str):
            if value in self._aliases:
                return self._aliases[value]
            if ISO.match(value):
                return "<timestamp>"
        return value

    def record(self, label: str, status: int, body: Any) -> None:
        self.steps.append({"step": label, "status": status, "body": self.scrub(body)})


def conversation_lifecycle(request: Callable[..., tuple[int, Any]]) -> list[Any]:
    """Create, rename, pin, archive, list, open and delete a conversation.

    `request(method, path, json=None)` must return (status_code, parsed_body).
    """
    t = Transcript()

    # The very first read lazily creates the fresh-install conversation. Alias it
    # up front so its (randomly generated) id does not show up as a spurious
    # difference in every later listing.
    status, body = request("GET", "/api/conversations")
    t.alias(body["activeId"], "bootstrap")
    t.record("bootstrap", status, body)

    status, body = request("POST", "/api/conversations")
    conv_id = body["conversation"]["id"]
    t.alias(conv_id, "conv1")
    t.record("create", status, body)

    status, body = request("GET", f"/api/conversations/{conv_id}")
    t.record("openFresh", status, body)

    status, body = request("PATCH", f"/api/conversations/{conv_id}", {"title": "  Renamed here  "})
    t.record("rename", status, body)

    # An empty title is rejected with a 400 and a plain message.
    status, body = request("PATCH", f"/api/conversations/{conv_id}", {"title": "   "})
    t.record("renameEmpty", status, body)

    status, body = request("PATCH", f"/api/conversations/{conv_id}", {"pinned": True})
    t.record("pin", status, body)

    # Several fields in one PATCH are applied in title/pinned/archived order.
    status, body = request(
        "PATCH", f"/api/conversations/{conv_id}", {"title": "Multi", "pinned": False, "archived": True}
    )
    t.record("multiPatch", status, body)

    status, body = request("PATCH", f"/api/conversations/{conv_id}", {"archived": False})
    t.record("unarchive", status, body)

    # A mixed PATCH where the title is invalid. This is the ONE case where the
    # order the fields are applied in is observable: title is validated first and
    # throws, so whether `pinned` was already written before the 400 depends
    # entirely on ordering. Without this step a reordered implementation passes
    # every other check, because each setter returns the whole fresh row and the
    # end state is identical when nothing fails.
    status, body = request(
        "PATCH", f"/api/conversations/{conv_id}", {"title": "   ", "pinned": True}
    )
    t.record("mixedInvalidPatch", status, body)

    status, body = request("GET", f"/api/conversations/{conv_id}")
    t.record("stateAfterMixedInvalidPatch", status, body["conversation"])

    # A second conversation, so ordering and activation have something to move between.
    status, body = request("POST", "/api/conversations")
    second_id = body["conversation"]["id"]
    t.alias(second_id, "conv2")
    t.record("createSecond", status, body)

    status, body = request("POST", f"/api/conversations/{conv_id}/activate")
    t.record("activateFirst", status, body)

    status, body = request("GET", "/api/conversations")
    t.record("listAfterActivate", status, body)

    # Every not-found path, on every verb.
    for method, path, payload in [
        ("GET", "/api/conversations/nope-zzz", None),
        ("PATCH", "/api/conversations/nope-zzz", {"title": "x"}),
        ("DELETE", "/api/conversations/nope-zzz", None),
        ("POST", "/api/conversations/nope-zzz/activate", None),
    ]:
        status, body = request(method, path, payload)
        t.record(f"missing:{method}", status, body)

    # Deleting the ACTIVE conversation must leave a fresh one active, never none.
    status, body = request("DELETE", f"/api/conversations/{conv_id}")
    t.record("deleteActive", status, body)

    status, body = request("GET", "/api/conversations")
    # The replacement conversation's id is minted during the delete, so it has no
    # alias — assert only that one exists and is active, which is the real
    # contract ("the chat screen always has somewhere to go").
    active = body.get("activeId")
    t.record(
        "listAfterDelete",
        status,
        {
            "count": len(body["conversations"]),
            "activeIsPresent": bool(active),
            "activeIsDeleted": active == conv_id,
            "titles": sorted(c["title"] for c in body["conversations"]),
        },
    )

    status, body = request("POST", "/api/reset")
    t.record("resetAliasRoute", status, {"ok": body.get("ok"), "hasConversation": bool(body.get("conversation"))})

    return t.steps


SCENARIOS = {"conversation_lifecycle": conversation_lifecycle}
