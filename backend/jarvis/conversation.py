"""Neutral, model-agnostic conversation store.

A port of server/conversation.js. One canonical transcript per session; the
adapters translate it to and from each model's own wire format. This is what
lets a mid-conversation model switch preserve context, instead of each provider
hoarding its own private history format and switching meaning starting over.

A neutral message:
    {id, role: 'user'|'assistant'|'tool', text?,
     toolCalls?: [{id, name, args}], toolResults?: [{id, name, result}],
     modelId?, raw?: {adapter, content},
     media?: [{kind: 'image'|'video', mimeType, dataBase64?, uri?}]}

`raw` carries a model's own reply object verbatim when its adapter needs exact
round-tripping (Gemini's thought_signature). `media` (user messages only) carries
images/video alongside text.

This in-memory dict is the trimmed 60-message working window a model actually
sees. A "bound" session ADDITIONALLY persists every message to chat_store.py,
which keeps the FULL transcript forever — the 60-entry cap here is a
context-window limit, not a retention policy.
"""

from __future__ import annotations

import threading
from typing import Any

from . import chat_store
from .jscompat import now_iso

MAX_HISTORY_ENTRIES = 60

_sessions: dict[str, list[dict[str, Any]]] = {}
_bound_sessions: set[str] = set()
_next_id = 1
# The Node original is single-threaded by virtue of the event loop. Uvicorn runs
# sync endpoint functions in a thread pool, so the shared id counter and the
# session dict need real mutual exclusion here or two concurrent turns can hand
# out the same message id.
_lock = threading.RLock()


def _new_id() -> str:
    global _next_id
    with _lock:
        value = _next_id
        _next_id += 1
    return f"m{value}"


def _safe_cut_index(messages: list[dict[str, Any]], target: int) -> int:
    """A tail slice that never cuts between an assistant `toolCalls` message and
    its matching `tool` result.

    A blind slice can land exactly there, and every adapter then replays an
    orphaned function/tool call on the next turn, which every provider's API
    rejects outright. Walks backward from the target cut point to the nearest
    safe boundary rather than cutting exactly at N. This can keep a handful more
    than MAX_HISTORY_ENTRIES messages during a run of back-to-back tool steps —
    an acceptable trade against handing a model a structurally invalid
    transcript.
    """
    i = target
    while i > 0 and messages[i]["role"] == "tool":
        i -= 1
    return i


def _trim(session_id: str) -> None:
    messages = _sessions.get(session_id)
    if not messages or len(messages) <= MAX_HISTORY_ENTRIES:
        return
    cut = _safe_cut_index(messages, len(messages) - MAX_HISTORY_ENTRIES)
    _sessions[session_id] = messages[cut:]


def _push(session_id: str, message: dict[str, Any]) -> dict[str, Any]:
    # createdAt (real wall-clock time, not the model's business) is what lets the
    # system prompt tell the model how long it has actually been since the
    # previous turn. Added here rather than left to chat_store's own created_at
    # so it is available on every session, bound or not — an ephemeral scheduled
    # task session has no chat-store row at all.
    #
    # The caller's own keys win over these defaults, matching the JS spread order
    # `{id: newId(), createdAt: ..., ...message}`.
    full = {"id": _new_id(), "createdAt": now_iso(), **message}
    with _lock:
        messages = _sessions.setdefault(session_id, [])
        messages.append(full)
        _trim(session_id)
        is_bound = session_id in _bound_sessions
    if is_bound:
        # Persist the neutral message as-is, minus the in-memory-only id (which
        # chat_store assigns its own row-based version of on read) and createdAt
        # (already stamped as its own column; storing it in the payload too would
        # double-store the same timestamp).
        rest = {k: v for k, v in full.items() if k not in ("id", "createdAt")}
        chat_store.append_message(session_id, rest)
    return full


def bind_session(session_id: str) -> None:
    """Mark a session as persistent — every message pushed from now on is also
    written to chat_store. Scheduled task runs and briefings deliberately never
    call this, so they stay ephemeral and never appear as browsable
    conversations."""
    with _lock:
        _bound_sessions.add(session_id)


def hydrate(session_id: str) -> None:
    """Load a persisted conversation's tail back into the working set.

    Called on startup (restoring the active conversation after a restart) and
    whenever the user opens an older conversation from Chat History. Also binds
    the session, since anything worth hydrating is worth persisting.
    """
    bind_session(session_id)
    stored = []
    for message in chat_store.get_messages(session_id):
        rest = {k: v for k, v in message.items() if k != "id"}
        stored.append({"id": _new_id(), **rest})
    with _lock:
        _sessions[session_id] = stored
        _trim(session_id)


def get_messages(session_id: str = "main") -> list[dict[str, Any]]:
    """All neutral messages in a session, oldest first."""
    return _sessions.get(session_id, [])


def load_snapshot(session_id: str, messages: list[dict[str, Any]] | None = None) -> None:
    """Restore a session's message list wholesale from a plain snapshot.

    Deliberately NOT hydrate(): never binds to chat_store, so a rehydrated job
    session still never appears in Chat History.
    """
    with _lock:
        _sessions[session_id] = [{**m, "id": _new_id()} for m in (messages or [])]


def assistant_text_of(message: dict[str, Any]) -> str:
    """The text an assistant message should be treated as having actually said.

    `spokenText` if it was interrupted, its full `text` otherwise. Every adapter's
    message mapping uses this instead of reading `text` directly for an assistant
    turn, so a barge-in never leaves the model believing, on the NEXT turn, that
    it finished saying something it was cut off partway through.
    """
    if message.get("interrupted"):
        return message.get("spokenText") or ""
    return message.get("text") or ""


def push_user_text(session_id: str, text: str, media: Any = None) -> dict[str, Any]:
    return _push(session_id, {"role": "user", "text": text, "media": media})


def push_assistant_text(
    session_id: str, text: str, model_id: str | None = None, raw: Any = None
) -> dict[str, Any]:
    return _push(session_id, {"role": "assistant", "text": text, "modelId": model_id, "raw": raw})


def push_assistant_tool_calls(
    session_id: str,
    tool_calls: list[dict[str, Any]],
    model_id: str | None = None,
    raw: Any = None,
    text: str | None = None,
) -> dict[str, Any]:
    return _push(
        session_id,
        {"role": "assistant", "toolCalls": tool_calls, "modelId": model_id, "raw": raw, "text": text},
    )


def push_tool_results(session_id: str, tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    return _push(session_id, {"role": "tool", "toolResults": tool_results})


def mark_last_assistant_interrupted(session_id: str, spoken_text: str) -> bool:
    """Mark the most recent assistant message as interrupted (barge-in).

    `spoken_text` is what the user actually heard, reconstructed from chunks that
    truly started playing — NOT a slice of the full text by character count,
    which would need exact whitespace agreement between the client's chunking and
    the model's own spacing that is not guaranteed to hold.

    The full generated `text` is left untouched (chat history can still show what
    the model would have finished saying); only `interrupted`/`spokenText` are
    added.

    Returns False if there is no assistant message to mark yet. That race is a
    known, disclosed gap: rare, and its failure mode is only "the interruption
    was not recorded", never wrong or corrupted data.
    """
    messages = _sessions.get(session_id)
    if not messages:
        return False
    for message in reversed(messages):
        if message["role"] == "assistant" and message.get("text"):
            message["interrupted"] = True
            message["spokenText"] = spoken_text
            if session_id in _bound_sessions:
                chat_store.update_last_assistant_message(
                    session_id, {"interrupted": True, "spokenText": spoken_text}
                )
            return True
    return False


def remove_last_orphaned_tool_call(session_id: str) -> bool:
    """Remove the most recent message if it is an assistant tool-call turn with
    no matching tool-result after it.

    Left in place, this orphaned call would poison every later turn AND every
    fallback model: every adapter replays it as an unanswered tool call, which
    every provider's API rejects outright. A no-op if the last message is not
    actually an orphaned tool call, so callers can call it unconditionally after
    any turn failure without checking first.
    """
    messages = _sessions.get(session_id)
    if not messages:
        return False
    last = messages[-1]
    if last["role"] != "assistant" or not last.get("toolCalls"):
        return False
    messages.pop()
    if session_id in _bound_sessions:
        chat_store.remove_last_message_if_matches(session_id, "assistant")
    return True


def reset_session(session_id: str = "main") -> None:
    _sessions.pop(session_id, None)


def reset_all_sessions() -> None:
    _sessions.clear()


def reset_for_tests() -> None:
    """Test-only: clear sessions, bindings and the id counter together."""
    global _next_id
    with _lock:
        _sessions.clear()
        _bound_sessions.clear()
        _next_id = 1
