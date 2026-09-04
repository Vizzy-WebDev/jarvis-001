"""Chat History — browsing, opening, renaming, pinning, archiving, deleting.

A port of server/server.js's /api/conversations routes. Status codes and error
message TEXT are reproduced exactly: the front end shows these strings to the
user directly, so a reworded 404 is a user-visible change, not an internal one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from .. import chat_store
from ..session import activate_conversation, get_active_session_id, reset_conversation

router = APIRouter(prefix="/api")

GONE = "That conversation no longer exists."


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": GONE})


@router.get("/conversations")
def list_conversations(
    q: str = Query(default=""),
    archived: str = Query(default=""),
) -> dict[str, Any]:
    include_archived = archived == "1"
    # get_active_session_id() must run BEFORE list_conversations(): on a fresh
    # install it lazily creates the very first conversation, and evaluating the
    # list first would miss it — a real bug found live in the original, showing
    # an empty list alongside a real activeId.
    active_id = get_active_session_id()
    return {
        "conversations": chat_store.list_conversations(query=q, include_archived=include_archived),
        "activeId": active_id,
    }


@router.post("/conversations")
def new_conversation() -> dict[str, Any]:
    return {"conversation": reset_conversation()}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    if not chat_store.is_conversation(conversation_id):
        return _not_found()
    return {
        "conversation": chat_store.get_conversation(conversation_id),
        "messages": chat_store.get_messages(conversation_id),
    }


@router.patch("/conversations/{conversation_id}")
def patch_conversation(conversation_id: str, patch: dict[str, Any] = Body(default_factory=dict)):
    if not chat_store.is_conversation(conversation_id):
        return _not_found()
    patch = patch or {}
    conversation: dict[str, Any] | None = None
    try:
        # Each field is applied independently and in this order, matching the
        # original: a single PATCH may carry any combination of them.
        if "title" in patch:
            conversation = chat_store.rename_conversation(conversation_id, patch["title"])
        if "pinned" in patch:
            conversation = chat_store.set_pinned(conversation_id, bool(patch["pinned"]))
        if "archived" in patch:
            conversation = chat_store.set_archived(conversation_id, bool(patch["archived"]))
    except ValueError as err:
        return JSONResponse(
            status_code=400,
            content={"error": str(err) or "Could not update that conversation."},
        )
    return {"conversation": conversation or chat_store.get_conversation(conversation_id)}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    if not chat_store.is_conversation(conversation_id):
        return _not_found()
    was_active = conversation_id == get_active_session_id()
    chat_store.delete_conversation(conversation_id)
    # Deleting the conversation you are currently in leaves nothing active —
    # start a fresh one so the chat screen always has somewhere to go.
    if was_active:
        reset_conversation()
    return {"ok": True}


@router.post("/conversations/{conversation_id}/activate")
def activate(conversation_id: str):
    try:
        return {"conversation": activate_conversation(conversation_id)}
    except LookupError as err:
        return JSONResponse(status_code=404, content={"error": str(err) or GONE})


@router.post("/reset")
def reset() -> dict[str, Any]:
    """A plain alias for "start a new chat".

    Nothing in the UI calls this any more (POST /api/conversations is what the
    New Chat button uses), but it is a small, harmless surface to leave working.
    """
    return {"ok": True, "conversation": reset_conversation()}
