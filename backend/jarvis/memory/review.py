"""The checkpoint engine — one batched extraction, never one per turn.

Nothing is extracted per turn, deliberately. A checkpoint is a moment where
something has actually wrapped up: a new chat, the assistant reopening, a
scheduled task finishing, or the model itself sensing a topic has closed. Doing
this per turn would roughly double model usage for a background feature nobody
asked for in that moment, on a roster where quota is the binding constraint.

An explicit "remember that X" never comes through here at all — it is filed the
moment the user confirms it, because that confirmation already IS the approval a
checkpoint exists to obtain.

Every call site is fire-and-forget: a checkpoint must never delay a reply or make
"new chat" feel slow.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import chat_store
from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..gateway.client import NoModelAvailable, ask
from ..gateway.routing import Task
from . import store
from .policy import AUTO_APPROVE, decide

logger = logging.getLogger(__name__)

SYSTEM = ("You extract durable, worth-remembering facts about a user from a piece of "
          "conversation or activity. You are careful and conservative, and you never "
          "invent anything that was not actually said.")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — so "Uses WSL for
    development work." and "uses wsl for development work" compare equal."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (text or "").strip().lower()))


def _is_duplicate(candidate_text: str, other_text: str) -> bool:
    """Bidirectional CONTAINMENT, not equality.

    Strict equality misses realistic paraphrasing — confirmed live, where an
    extraction wrote "User's sister is getting married in March." against a saved
    "Sister is getting married in March". Those never normalise equal, but one
    plainly contains the other.
    """
    a, b = normalize(candidate_text), normalize(other_text)
    return bool(a and b and (a in b or b in a))


def _prompt(transcript: str, memories: list[dict[str, Any]], categories: list[str],
            pending: list[dict[str, Any]]) -> str:
    memories_block = ("\n".join(f"- [{m['id']}] ({m['category']}) {m['text']}" for m in memories)
                      or "(none yet)")
    lines = [
        "Read the excerpt below and pull out any durable facts, preferences, or goals "
        "about the user that are worth remembering long-term.",
        "Skip anything trivial, one-off, or already obvious from context — only propose "
        "something a person would genuinely want recalled weeks from now.",
        "",
        f"Existing categories: {', '.join(categories)}. Use one of these if it fits; "
        "otherwise propose a short, clear new one.",
        "",
        "Existing memories. Do NOT propose anything already covered by one of these — not "
        "reworded, not re-confirmed. Only flag one via conflictsWithId if the excerpt "
        "genuinely CONTRADICTS it (a changed preference, an outdated fact); restating or "
        "recalling the same fact again is not a contradiction.",
        memories_block,
    ]
    if pending:
        lines += ["", "Already proposed and awaiting the user's own decision — don't propose "
                      "these again either, even reworded:",
                  "\n".join(f"- ({c['category']}) {c['text']}" for c in pending)]
    lines += [
        "", "Excerpt:", transcript, "",
        'Reply with JSON: {"candidates": [{"text": "...", "category": "...", '
        '"conflictsWithId": "mem_id or null", "confidence": 0.0-1.0, '
        '"importance": 1-5 or null, "expiresAt": "ISO date or null"}]}.',
        '"confidence": how sure you are this is a real, durable fact the user would want '
        "kept. 0.9+ only when they stated it plainly and directly about themselves; "
        "0.6-0.8 when it is clearly implied but not said outright; below 0.5 when you are "
        "inferring or unsure. Be honest and conservative — most things are not 0.9. This "
        "number controls whether the fact is saved automatically or held for the user to "
        "review, so an inflated score is not a harmless guess.",
        '"importance": 1 (trivia) to 5 (would change how you help them). Null if you '
        "genuinely cannot tell — a made-up number is worse than none.",
        '"expiresAt": only for something that stops being true on a known date ("in Lisbon '
        'until the 14th"). Null for anything ordinary — most facts do not expire.',
        "An empty candidates array is a completely normal answer — most excerpts have "
        "nothing worth remembering. Never invent a fact that is not actually stated or "
        "clearly implied.",
    ]
    return "\n".join(lines)


def _clamp(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(low, min(high, float(value)))


def extract_and_file(transcript: str, *, conversation_id: str | None = None,
                     source_kind: str, source_ref: str | None = None,
                     event_bus: EventBus | None = None) -> dict[str, list[dict[str, Any]]]:
    """One model call over a chunk of new content; files whatever it finds."""
    ebus = event_bus or default_bus
    memories = store.list_memories()
    categories = [c["name"] for c in store.list_categories(include_pending=False)]
    # The model must see what is already PENDING too, or a fact still sitting in
    # the review queue gets re-proposed the next time the topic comes up.
    pending_before = store.list_pending_candidates()

    try:
        answer = ask(
            _prompt(transcript, memories, categories, pending_before),
            system=SYSTEM, want_json=True,
            # Nothing is waiting on this, so cost matters more than latency.
            task=Task(text="extract durable facts", background=True, needs_tools=False),
        )
    except NoModelAvailable as err:
        # Losing the material silently is the failure mode that matters here: the
        # checkpoint pointer is NOT advanced by this function, so the same
        # messages are looked at again next time rather than being dropped.
        logger.warning("memory checkpoint skipped — no model available: %s", err)
        return {"candidates": [], "autoSaved": [], "skipped": True}

    data = answer.data if isinstance(answer.data, dict) else {}
    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list):
        return {"candidates": [], "autoSaved": []}

    pending: list[dict[str, Any]] = []
    auto_saved: list[dict[str, Any]] = []

    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue

        category = str(raw.get("category") or "Uncategorized").strip() or "Uncategorized"
        if category not in categories:
            # A category the model just invented is never automatically
            # permanent: it rides in the same approval batch as its candidates.
            store.propose_category(category)

        conflict_id = raw.get("conflictsWithId")
        conflict_with = conflict_id if any(m["id"] == conflict_id for m in memories) else None

        # A deterministic backstop, independent of the model following the
        # "don't propose what's already covered" instruction: checked against
        # EVERY memory and pending candidate, not just whichever one the model
        # happened to flag. Without it, the assistant recalling a saved fact out
        # loud gets re-extracted as new evidence and misfiled as a conflict
        # against the very memory it duplicates — confirmed live.
        if (any(_is_duplicate(text, m["text"]) for m in memories)
                or any(_is_duplicate(text, c["text"]) for c in pending_before)):
            continue

        confidence = _clamp(raw.get("confidence"), 0.0, 1.0)
        importance = _clamp(raw.get("importance"), 1.0, 5.0)
        expires_at = raw.get("expiresAt") if isinstance(raw.get("expiresAt"), str) else None

        candidate = store.create_candidate(
            conversation_id=conversation_id, source_kind=source_kind, source_ref=source_ref,
            category=category, text=text, conflict_with=conflict_with, confidence=confidence)

        verdict = decide({"confidence": candidate["confidence"],
                          "conflictsWithId": candidate["conflictWith"]})
        if verdict == AUTO_APPROVE:
            memory = store.auto_approve_candidate(candidate["id"])
            if importance is not None or expires_at:
                memory = store.update_memory(memory["id"], importance=int(importance)
                                             if importance is not None else None,
                                             expires_at=expires_at, reason="Extracted.")
            auto_saved.append(memory)
        else:
            pending.append(candidate)

    if pending:
        ebus.publish(EventType.NOTIFICATION_CREATED, {
            "kind": "memory", "level": "info",
            "title": "There's something to review in Memory",
            "count": len(pending),
            "candidates": [{"id": c["id"], "text": c["text"], "category": c["category"]}
                           for c in pending]})
    if auto_saved:
        # A fact saved without being asked still has to be visible and undoable.
        ebus.publish(EventType.NOTIFICATION_CREATED, {
            "kind": "memory", "level": "info",
            "title": ("Jarvis remembered something on its own" if len(auto_saved) == 1
                      else f"Jarvis remembered {len(auto_saved)} things on its own"),
            "body": " · ".join(m["text"] for m in auto_saved),
            "action": {"label": "Review Memory", "section": "memory"}})

    return {"candidates": pending, "autoSaved": auto_saved}


def checkpoint_conversation(conversation_id: str, reason: str = "unspecified",
                            event_bus: EventBus | None = None) -> dict[str, Any]:
    """Read only what is NEW since this conversation's last checkpoint.

    A repeated checkpoint on a quiet conversation therefore costs nothing.
    """
    if not conversation_id or not chat_store.is_conversation(conversation_id):
        return {"candidates": []}

    last_seq = store.get_checkpoint(conversation_id)
    since = chat_store.get_messages_since(conversation_id, last_seq)
    relevant = [m for m in since if m.get("role") in ("user", "assistant") and m.get("text")]

    if not relevant:
        # Still advance the pointer: those messages WERE looked at and correctly
        # judged to hold nothing worth remembering.
        if since:
            store.set_checkpoint(conversation_id, since[-1]["seq"])
        return {"candidates": []}

    # An assistant turn is labelled as a RECALL rather than a fresh statement.
    # This is the actual cause of a live duplicate-conflict loop: when Jarvis
    # says a saved fact back out loud, a plain "Jarvis: ..." label gives the
    # extraction model no way to tell that apart from new information.
    transcript = "\n".join(
        f"User: {m['text']}" if m["role"] == "user"
        else f"Jarvis (recalling something already remembered — not the user newly "
             f"stating it): {m['text']}"
        for m in relevant)

    result = extract_and_file(transcript, conversation_id=conversation_id,
                             source_kind="chat", source_ref=conversation_id,
                             event_bus=event_bus)
    if not result.get("skipped"):
        store.set_checkpoint(conversation_id, since[-1]["seq"])
    return result


def checkpoint_from_text(text: str, *, source_kind: str, source_ref: str | None = None,
                         event_bus: EventBus | None = None) -> dict[str, Any]:
    """A checkpoint over plain text rather than a stored conversation — a
    scheduled task's own result, or any other non-chat source. Every candidate
    already carries a generic source_kind/source_ref, so this needs no schema
    change to cover a new one."""
    body = (text or "").strip()
    if not body:
        return {"candidates": []}
    return extract_and_file(body, conversation_id=None, source_kind=source_kind,
                            source_ref=source_ref, event_bus=event_bus)
