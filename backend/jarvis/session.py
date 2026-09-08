"""Which conversation is currently active, and switching between them.

The session-management half of server/brain.js. The turn-running half (runTurn
and its merge-and-restart coordinator) belongs to the live turn engine and lands
with it in Wave 2 — splitting them here is deliberate: everything in this file is
pure store manipulation that Wave 1's routes need, and none of it depends on a
model being reachable.

`get_active_session_id()` is the ONE place the "which conversation is open"
decision is made, so every caller stays in sync with whatever the user has open,
including across a restart. The session id IS the active conversation's id — not
a hardcoded string.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import chat_store, conversation, session_hooks
from .memory.review import checkpoint_conversation

logger = logging.getLogger(__name__)

_active_id: str | None = None  # cached; chat_store's app_state table is the source of truth
_reopen_checkpoint_done = False  # the "next open" checkpoint fires once per process
_lock = threading.RLock()


def _fire_and_forget_checkpoint(conversation_id: str, reason: str) -> None:
    """Run a checkpoint without ever making the caller wait for it.

    A checkpoint reads a whole session's worth of conversation and spends a model
    call, so it must never delay the first request served after startup, and must
    never make "New chat" feel slow. Errors are logged and dropped — a failed
    checkpoint is not a failed user action.

    A plain thread rather than an asyncio task: the work underneath is genuinely
    blocking (a provider SDK call), so scheduling it on the event loop would
    block every other request for its duration. It also means this works
    identically from a sync context, where there is no running loop at all.
    """
    def _run() -> None:
        try:
            checkpoint_conversation(conversation_id, reason)
        except Exception:
            logger.exception("%s checkpoint failed", reason)

    threading.Thread(target=_run, name=f"checkpoint-{reason}", daemon=True).start()


def get_active_session_id() -> str:
    """The active conversation's id, creating the first one on a fresh install."""
    global _active_id, _reopen_checkpoint_done
    with _lock:
        if _active_id:
            return _active_id

        conversation_id = chat_store.get_active_id()
        is_fresh_install = not conversation_id or not chat_store.is_conversation(conversation_id)
        if is_fresh_install:
            created = chat_store.create_conversation()
            conversation_id = created["id"]
            chat_store.set_active_id(conversation_id)

        # Loads its persisted tail back into the in-memory working set.
        conversation.hydrate(conversation_id)
        _active_id = conversation_id

        should_checkpoint = not is_fresh_install and not _reopen_checkpoint_done
        if should_checkpoint:
            _reopen_checkpoint_done = True

    # The "next open" checkpoint — covers "I closed Jarvis" without depending on
    # catching an unload event, which is not reliable. Fires once, the first time
    # the active conversation is actually resumed; a fresh install has nothing to
    # check yet. Started outside the lock so a slow checkpoint cannot block
    # another request resolving the active id.
    if should_checkpoint:
        _fire_and_forget_checkpoint(conversation_id, "reopen")

    return conversation_id


def reset_conversation() -> dict[str, Any]:
    """Start a new chat, checkpointing the one being left behind."""
    global _active_id
    previous_id = _active_id or get_active_session_id()

    # The most reliable of the checkpoints, since the user just clicked
    # something. Fire-and-forget: starting a new chat must feel instant, not wait
    # on a background model call over the conversation being left behind.
    _fire_and_forget_checkpoint(previous_id, "new_chat")

    # Clears the transcript AND every other piece of per-session state that
    # must not survive a new chat — the sticky model pick, the unlocked-tool set,
    # the sticky style request. Those live in Wave 2 modules and register
    # themselves; see session_hooks for why this is a registry rather than a
    # remembered list of calls.
    conversation.reset_session(previous_id)
    session_hooks.run_session_resets(previous_id)

    conv = chat_store.create_conversation()
    chat_store.set_active_id(conv["id"])
    conversation.bind_session(conv["id"])
    with _lock:
        _active_id = conv["id"]
    return conv


def activate_conversation(conversation_id: str) -> dict[str, Any]:
    """Switch to an existing conversation and hydrate its transcript back into
    the working set, so the model has its context again.

    Raises LookupError if the id does not exist.
    """
    global _active_id
    if not chat_store.is_conversation(conversation_id):
        raise LookupError("That conversation no longer exists.")
    conversation.hydrate(conversation_id)
    chat_store.set_active_id(conversation_id)
    with _lock:
        _active_id = conversation_id
    return chat_store.get_conversation(conversation_id)


def reset_for_tests() -> None:
    global _active_id, _reopen_checkpoint_done
    with _lock:
        _active_id = None
        _reopen_checkpoint_done = False
