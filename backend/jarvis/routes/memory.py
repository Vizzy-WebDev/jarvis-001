"""Memory, and the profile notes that are one category of it.

A thin surface over `memory/store.py`, which already holds every rule about what
a memory is — versions on edit, the conflict flag, categories, the candidate
queue. Nothing here decides anything.

**Two rules from the subsystem below are visible in this file's shape.**

A conflict always needs a person, at every trust level, with no override —
resolving one changes or duplicates something that already exists, and that is
never done silently. So `resolve-conflict` is its own route rather than a flag on
approve: they are different acts and reading them as one is how the distinction
gets lost.

An approved memory can never be cascaded away by deleting a conversation — the
schema has no foreign key from one to the other — so there is no route here that
takes a conversation id. Deleting is deliberately two steps: archive, then
delete, because the screen's own undo depends on the row still existing.

**Profile is not a second store.** "Profile & Goals" is the `About You` category,
and these routes are the adapter — the same seam the original kept as its own
module. Order is the one difference worth stating: memories come back
newest-first, which is right for browsing, and these notes read oldest-first,
which is the order they were written in.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..memory import store

router = APIRouter(prefix="/api")

#: The one category the profile routes below are fixed to.
PROFILE_CATEGORY = "About You"


def _entry(memory: dict[str, Any]) -> dict[str, Any]:
    """A memory as a profile note. The shape the original's own screen reads."""
    return {"id": memory["id"], "text": memory["text"], "addedAt": memory["createdAt"]}


# --- browsing ------------------------------------------------------------------
#
# `candidates` and `categories` are declared BEFORE `{memory_id}`, and that
# ordering is load-bearing: a path parameter would otherwise swallow both and
# answer "no such memory" for a route that exists.

@router.get("/memories/candidates")
def candidates() -> dict[str, Any]:
    """Everything waiting to be reviewed. Nothing here has been saved yet."""
    return {"candidates": store.list_pending_candidates()}


@router.get("/memories/categories")
def categories() -> dict[str, Any]:
    return {"categories": store.list_categories()}


@router.get("/memories")
def listed(category: str | None = None, query: str | None = None,
           origin: str | None = None, includeArchived: bool = False) -> dict[str, Any]:
    """The memories, plus which of them a pending candidate contradicts.

    The conflicted set is a SIBLING key rather than a field on each row, so the
    recorded shape stays byte-identical and the addition is one top-level key.
    It matters on screen: a memory something disagrees with is not being asserted
    to the model as settled fact while it waits, and saying so is more use than
    showing it as though nothing were wrong.
    """
    memories = store.list_memories(category, query=query, origin=origin,
                                   include_archived=includeArchived)
    return {"memories": memories, "conflicted": sorted(store.conflicted_memory_ids())}


@router.get("/memories/{memory_id}/versions")
def versions(memory_id: str) -> dict[str, Any]:
    """Empty for an unknown id rather than a 404 — asking what changed about
    something that does not exist has a true answer: nothing did."""
    return {"versions": store.version_history(memory_id)}


# --- editing -------------------------------------------------------------------

@router.post("/memories")
def create(body: dict[str, Any] = Body(default_factory=dict)):
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "No text given."}, status_code=400)
    memory = store.create_memory(
        category=str(body.get("category") or "Uncategorized"), text=text,
        source_kind=body.get("sourceKind") or "chat",
        # Written by hand, on purpose, by the person it is about. That IS the
        # consent a review exists to obtain, which is what `explicit` records.
        origin="explicit", importance=body.get("importance"))
    return {"ok": True, "memory": memory}


@router.patch("/memories/{memory_id}")
def edit(memory_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    try:
        memory = store.update_memory(
            memory_id, text=body.get("text"), category=body.get("category"),
            importance=body.get("importance"),
            reason=str(body.get("reason") or "Edited from the Memory screen."))
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown memory."}, status_code=404)
    return {"ok": True, "memory": memory}


@router.post("/memories/merge")
def merge(body: dict[str, Any] = Body(default_factory=dict)):
    """Fold duplicates into one, keeping the primary and archiving the rest.

    Archiving rather than deleting the others is deliberate: a merge that turns
    out to be wrong should be recoverable, and the version row on the primary
    records what it became.
    """
    primary = str(body.get("primaryId") or "")
    others = [str(i) for i in (body.get("otherIds") or [])]
    text = str(body.get("text") or "").strip()
    if not primary or not text:
        return JSONResponse({"ok": False, "error": "A merge needs a primary and the merged text."},
                            status_code=400)
    try:
        memory = store.merge_memories(primary, others, text, body.get("category"))
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown memory."}, status_code=404)
    return {"ok": True, "memory": memory}


@router.post("/memories/{memory_id}/archive")
def archive(memory_id: str) -> dict[str, Any]:
    store.archive_memory(memory_id)
    return {"ok": True}


@router.post("/memories/{memory_id}/restore")
def restore(memory_id: str) -> dict[str, Any]:
    store.restore_memory(memory_id)
    return {"ok": True}


@router.delete("/memories/{memory_id}")
def remove(memory_id: str) -> dict[str, Any]:
    store.delete_memory(memory_id)
    return {"ok": True}


# --- the review queue ----------------------------------------------------------

@router.post("/memories/candidates/{candidate_id}/approve")
def approve(candidate_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """Save it, optionally with edits made while reading it.

    Every one of these is a direct write with no model call, so a review button
    costs no quota and answers instantly — which is what makes reviewing a queue
    of them bearable.
    """
    try:
        memory = store.approve_candidate(candidate_id, {
            k: v for k, v in body.items() if k in ("text", "category", "importance")})
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown candidate."}, status_code=404)
    return {"ok": True, "memory": memory}


@router.post("/memories/candidates/{candidate_id}/reject")
def reject(candidate_id: str) -> dict[str, Any]:
    store.reject_candidate(candidate_id)
    return {"ok": True}


@router.post("/memories/candidates/{candidate_id}/resolve-conflict")
def resolve_conflict(candidate_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """`keep-old`, `use-new`, or `keep-both`.

    `use-new` EDITS the contradicted memory rather than adding a second one: two
    memories asserting opposite things is the state this whole path exists to
    prevent.
    """
    choice = str(body.get("choice") or "")
    if choice not in ("keep-old", "use-new", "keep-both"):
        return JSONResponse({"ok": False, "error": "Choose keep-old, use-new or keep-both."},
                            status_code=400)
    try:
        memory = store.resolve_conflict(candidate_id, choice, {
            k: v for k, v in body.items() if k in ("text", "category")})
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown candidate."}, status_code=404)
    return {"ok": True, "memory": memory}


# --- profile notes -------------------------------------------------------------

@router.get("/profile")
def profile() -> dict[str, Any]:
    entries = [_entry(m) for m in store.list_memories(PROFILE_CATEGORY)]
    entries.sort(key=lambda e: e["addedAt"] or "")
    return {"entries": entries}


@router.post("/profile")
def add_note(body: dict[str, Any] = Body(default_factory=dict)):
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "No text given."}, status_code=400)
    memory = store.create_memory(category=PROFILE_CATEGORY, text=text,
                                 source_kind="chat", origin="explicit")
    return {"ok": True, "entry": _entry(memory)}


@router.patch("/profile/{entry_id}")
def edit_note(entry_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "No text given."}, status_code=400)
    try:
        memory = store.update_memory(entry_id, text=text,
                                     reason="Edited from Profile & Goals.")
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown note."}, status_code=404)
    return {"ok": True, "entry": _entry(memory)}


@router.get("/profile/{entry_id}/versions")
def note_versions(entry_id: str) -> dict[str, Any]:
    return {"versions": [{"text": v["text"], "changedAt": v["changedAt"],
                          "reason": v.get("reason")}
                         for v in store.version_history(entry_id)]}


@router.delete("/profile/{entry_id}")
def delete_note(entry_id: str) -> dict[str, Any]:
    store.delete_memory(entry_id)
    return {"ok": True}
