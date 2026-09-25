"""Saving, correcting, forgetting and reviewing what Jarvis remembers.

All four write-side tools are MEDIUM risk, which is what makes them confirmed:
the confirmation comes from the risk classification rather than a per-tool flag,
so there is one mechanism deciding when a human is asked and not two that can
disagree. Each supplies its own read-back sentence — the policy can say why a
confirmation is needed, but only the tool knows what these arguments mean.

An explicit "remember that…" writes STRAIGHT into memory rather than joining the
candidate queue: the user's confirmation is the approval a checkpoint exists to
obtain, and asking again in a review card later is asking twice. That is why
`origin` is 'explicit' here and not 'approved'.
"""

from __future__ import annotations

import re
from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..memory import review, store

CATEGORY = "About You"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (text or "").strip().lower()))


def find_similar(text: str) -> dict[str, Any] | None:
    """Bidirectional containment against what is already saved.

    Not a merge and not a block — it gives the read-back a chance to say "this
    looks like something you already have", so the user can choose to update
    rather than ending up with two notes saying nearly the same thing. The
    conflict floor only runs on the quiet checkpoint path; this direct path has
    nothing else watching it.
    """
    target = _normalize(text)
    if not target:
        return None
    for memory in store.list_memories():
        existing = _normalize(memory["text"])
        if existing and (existing in target or target in existing):
            return memory
    return None


# --- remember ----------------------------------------------------------------

def _remember(text: str = "") -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "There's nothing to remember."}
    memory = store.create_memory(category=CATEGORY, text=body, source_kind="chat",
                                 origin="explicit")
    return {"ok": True, "saved": memory["text"], "speak": f"Noted: {memory['text']}"}


def _remember_summary(args: dict[str, Any]) -> str:
    text = str(args.get("text") or "").strip()
    similar = find_similar(text)
    if similar:
        return (f'Remember this about you: "{text}". That looks close to something '
                f'I already have — "{similar["text"]}". Save it as a new note anyway?')
    return f'Remember this about you: "{text}".'


# --- correct -----------------------------------------------------------------

def _find_match(query: str) -> dict[str, Any] | None:
    wanted = _normalize(query)
    if not wanted:
        return None
    memories = store.list_memories()
    for memory in memories:
        if _normalize(memory["text"]) == wanted:
            return memory
    scored = [(len(set(wanted.split()) & set(_normalize(m["text"]).split())), m)
              for m in memories]
    best = max(scored, key=lambda row: row[0], default=(0, None))
    return best[1] if best[0] > 0 else None


def _update(query: str = "", new_text: str = "") -> dict[str, Any]:
    match = _find_match(query)
    if match is None:
        return {"ok": False, "error": f'I couldn\'t find anything matching "{query}" to update.'}
    updated = store.update_memory(match["id"], text=new_text,
                                  reason="Updated via conversation.", origin="explicit")
    return {"ok": True, "before": match["text"], "after": updated["text"]}


def _update_summary(args: dict[str, Any]) -> str:
    match = _find_match(str(args.get("query") or ""))
    if match is None:
        return f'I couldn\'t find anything matching "{args.get("query")}" to update.'
    return f'Change "{match["text"]}" to "{args.get("new_text")}"?'


# --- forget ------------------------------------------------------------------

def _forget(query: str = "") -> dict[str, Any]:
    match = _find_match(query)
    if match is None:
        return {"ok": False, "error": f'I couldn\'t find anything matching "{query}".'}
    # Archived, not deleted: forgetting should be undoable, and the Memory screen
    # is where a real deletion happens with the row in front of the user.
    store.archive_memory(match["id"])
    return {"ok": True, "forgot": match["text"],
            "note": "Archived rather than deleted, so it can be brought back."}


def _forget_summary(args: dict[str, Any]) -> str:
    match = _find_match(str(args.get("query") or ""))
    if match is None:
        return f'I couldn\'t find anything matching "{args.get("query")}".'
    return f'Forget "{match["text"]}"?'


# --- read-only ---------------------------------------------------------------

def _review() -> dict[str, Any]:
    pending = store.list_pending_candidates()
    if not pending:
        return {"ok": True, "pending": [], "note": "Nothing is waiting to be reviewed."}
    return {"ok": True, "pending": [
        {"id": c["id"], "text": c["text"], "category": c["category"],
         "conflictsWith": (store.get_memory(c["conflictWith"]) or {}).get("text")
         if c["conflictWith"] else None}
        for c in pending]}


def _checkpoint(reason: str = "a topic wrapped up") -> dict[str, Any]:
    from ..session import get_active_session_id

    # Fire and forget: a checkpoint spends a model call and reads a whole
    # session, and it must never make the reply the user is waiting for slow.
    import threading

    session_id = get_active_session_id()
    threading.Thread(target=lambda: review.checkpoint_conversation(session_id, reason),
                     name="checkpoint-tool", daemon=True).start()
    return {"ok": True, "note": "Noted quietly in the background.",
            "spoken_hint": "Don't mention this — it is a background action."}


SPECS = [
    CapabilitySpec(
        id="builtin.remember_about_me", name="remember_about_me",
        description=("Save a note the user has DIRECTLY ASKED to be remembered about them — "
                     "their work, goals, or preferences. Only for an explicit request: "
                     "\"remember that…\", \"note this down\", \"don't forget I…\". Never call "
                     "this because something merely came up in conversation and seemed worth "
                     "keeping — facts mentioned in passing are captured separately in the "
                     "background and must not be turned into a confirmation question."),
        input_schema={"type": "object", "properties": {
            "text": {"type": "string",
                     "description": "The note to remember, as a short clear sentence."}},
            "required": ["text"]},
        # MEDIUM, not LOW: this changes what the assistant will assert about the
        # user later, and a misheard note becomes a wrong "fact" repeated back.
        risk=Risk.MEDIUM, handler=_remember, summarize=_remember_summary,
        timeout_s=10.0, tags=frozenset({"core", "meta"}),
    ),
    CapabilitySpec(
        id="builtin.update_memory", name="update_memory",
        description=("Change what Jarvis remembers about something — a fact that has changed, "
                     "a correction. Use this ONLY when the user actually asks for the "
                     "correction, rather than remember_about_me again, which would add a "
                     "second, conflicting note instead of fixing the existing one. Never call "
                     "it just because something they said disagrees with a note — that is "
                     "caught separately in the background and goes back to them to decide."),
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "What to find, in the user's own words."},
            "new_text": {"type": "string", "description": "What it should say now."}},
            "required": ["query", "new_text"]},
        risk=Risk.MEDIUM, handler=_update, summarize=_update_summary,
        timeout_s=10.0, tags=frozenset({"core", "meta"}),
    ),
    CapabilitySpec(
        id="builtin.forget_something", name="forget_something",
        description="Forget something Jarvis remembers, when the user asks it to.",
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "What to forget, in their own words."}},
            "required": ["query"]},
        risk=Risk.MEDIUM, handler=_forget, summarize=_forget_summary,
        timeout_s=10.0, tags=frozenset({"core", "meta"}),
    ),
    CapabilitySpec(
        id="builtin.review_memories", name="review_memories",
        description=("Show what is waiting to be reviewed in Memory — things Jarvis noticed "
                     "that need the user's decision. Use when they ask what is pending."),
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.LOW, handler=_review, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.checkpoint_memories", name="checkpoint_memories",
        description=("Call once when a piece of work or a topic has clearly wrapped up, so "
                     "anything worth remembering is noticed quietly. Never for a short reply, "
                     "never announced, and never twice for the same stretch of conversation."),
        input_schema={"type": "object", "properties": {
            "reason": {"type": "string", "description": "Briefly, what wrapped up."}},
            "required": []},
        # Read-and-propose only: nothing is saved without either clearing the
        # trust threshold or the user approving a card.
        risk=Risk.LOW, handler=_checkpoint, timeout_s=5.0, tags=frozenset({"meta"}),
    ),
]
