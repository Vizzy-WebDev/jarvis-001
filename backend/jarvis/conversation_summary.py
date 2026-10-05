"""A running summary of each conversation: what has scrolled out of the turns sent in full.

Without it, anything that no longer fit the answering model's room simply vanished, and
"what did we decide?" or "the second one" had nothing to point at. The approach is the
one the research converges on (OpenAI's session-memory cookbook, LangGraph, Anthropic's
context engineering): keep recent turns word for word, and fold older turns into
structured notes — never a free-form paraphrase — written only from the transcript.

**Sized by the model, not by a number here.** A fold happens when the conversation's
verbatim history passes a share of the room the answering model actually had on the turn
that just ended (`contextBudget` on `ASSISTANT_RESPONSE`, from `orchestrator/context.
budget_for`). A big model therefore keeps far more word for word and summarizes rarely; a
small one summarizes sooner. A model that never said its size gives no budget, and then
only what is leaving Jarvis's in-memory working set is folded, so nothing is ever lost.

**Anchored on the saved `seq`.** `covered_seq` is the last saved message the summary
accounts for; the assembler sends everything after it as itself. Folds are always whole
turns, so the boundary never separates a tool call from its result. Edit/Retry cutting into
the covered range drops the summary (`chat_store.truncate_to_before`).

Runs after a reply, off the turn's thread (`background.run_in_background`), one at a time
per conversation, behind its own interlock like every other background subsystem.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from . import background, chat_store
from .db import get_db
from .jscompat import now_iso

logger = logging.getLogger(__name__)

ENABLE_ENV = "JARVIS_CONVERSATION_SUMMARY"

#: Verbatim history above this share of the answering model's budget gets folded…
FOCUS_SHARE = 0.5
#: …until it is back under this share of that target, so a fold happens every several
#: turns rather than on every turn.
REFOLD_SHARE = 0.6
#: The latest exchanges are never folded: "that", "the second one" point at them.
KEEP_RECENT_TURNS = 2

#: How long one note and one list may be, so a summary stays notes rather than a transcript.
MAX_ITEMS = 12
MAX_ITEM_CHARS = 300
TEXT_FIELDS = ("goal", "current")
LIST_FIELDS = ("facts", "decisions", "open", "parked")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "description": "What this conversation is working towards, or empty."},
        "facts": {"type": "array", "items": {"type": "string"},
                  "description": "Things the person said that still matter for the rest of the conversation."},
        "decisions": {"type": "array", "items": {"type": "string"},
                      "description": "What was decided or agreed, and what was finished."},
        "open": {"type": "array", "items": {"type": "string"},
                 "description": "Unanswered questions, unfinished requests, and anything Jarvis said it would do."},
        "parked": {"type": "array", "items": {"type": "string"},
                   "description": "Topics started and set aside, to come back to."},
        "current": {"type": "string", "description": "What was being discussed most recently."},
    },
    "required": ["goal", "facts", "decisions", "open", "parked", "current"],
}

SYSTEM = """You keep the running notes of a conversation between a person and their assistant, Jarvis.
You are given the notes so far (possibly empty) and the next part of the conversation. Return the updated notes.

Rules:
- Write only what the conversation actually says. Never infer, guess or add anything.
- Keep names, numbers, dates, file names, links and error messages exactly as written.
- Carry forward everything in the previous notes that still holds. When something in "open" was done or answered, move it to "decisions".
- "open" must include anything Jarvis said it would do and has not yet done.
- "parked" is for topics the person started and moved away from, so they can be picked up again.
- Short, plain notes. No commentary, no judgement of the person."""

_lock = threading.Lock()
_in_flight: set[str] = set()


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


# --- reading -----------------------------------------------------------------------------

def get(conversation_id: str) -> dict[str, Any] | None:
    """`{summary, coveredSeq, updatedAt}`, or None when this conversation has none."""
    try:
        row = get_db().execute(
            "SELECT summary, covered_seq, updated_at FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,)).fetchone()
    except Exception:  # noqa: BLE001 — a turn must never fail because of its notes
        logger.exception("could not read the summary of %s", conversation_id)
        return None
    if row is None:
        return None
    try:
        summary = json.loads(row["summary"])
    except ValueError:
        return None
    return {"summary": summary, "coveredSeq": row["covered_seq"], "updatedAt": row["updated_at"]}


def goal_of(conversation_id: str | None) -> str | None:
    if not conversation_id:
        return None
    found = get(conversation_id)
    goal = str(((found or {}).get("summary") or {}).get("goal") or "").strip()
    return goal or None


def render(summary: dict[str, Any]) -> str:
    """The notes as plain lines for the system prompt."""
    lines: list[str] = []
    if summary.get("goal"):
        lines.append(f"Working towards: {summary['goal']}")
    for field, heading in (("decisions", "Decided or done"), ("facts", "What they told you"),
                           ("open", "Still open (including anything you said you would do)"),
                           ("parked", "Set aside, to come back to")):
        items = [str(i) for i in summary.get(field) or [] if str(i).strip()]
        if items:
            lines.append(f"{heading}:")
            lines += [f"- {item}" for item in items]
    if summary.get("current"):
        lines.append(f"Most recently: {summary['current']}")
    return "\n".join(lines)


# --- deciding what to fold ---------------------------------------------------------------

def _turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Whole turns: each starts at a person's message and runs to the next one."""
    turns: list[list[dict[str, Any]]] = []
    for message in messages:
        if message.get("role") == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def plan_fold(messages: list[dict[str, Any]], budget_tokens: int | None) -> list[dict[str, Any]]:
    """Which of these not-yet-summarized messages to fold now — whole turns, oldest first,
    never the latest `KEEP_RECENT_TURNS`. Empty when nothing needs folding."""
    from .conversation import MAX_HISTORY_ENTRIES
    from .orchestrator.context import message_cost, shorten_old_tool_results

    turns = _turns(messages)
    if len(turns) <= KEEP_RECENT_TURNS:
        return []
    candidates = turns[:-KEEP_RECENT_TURNS]

    folded: list[list[dict[str, Any]]] = []
    if budget_tokens is None:
        # The model never said its size: fold only what is about to leave the
        # in-memory working set, which would otherwise vanish.
        leaving = max(0, len(messages) - MAX_HISTORY_ENTRIES)
        count = 0
        for turn in candidates:
            if count + len(turn) > leaving:
                break
            folded.append(turn)
            count += len(turn)
    else:
        target = budget_tokens * FOCUS_SHARE
        costs = [sum(message_cost(m) for m in shorten_old_tool_results(turn + [{"role": "user"}])[:-1])
                 for turn in turns]
        remaining = sum(costs)
        if remaining <= target:
            return []
        for turn, cost in zip(candidates, costs):
            if remaining <= target * REFOLD_SHARE:
                break
            folded.append(turn)
            remaining -= cost
    return [m for turn in folded for m in turn]


# --- writing ------------------------------------------------------------------------------

def _clip(text: Any) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= MAX_ITEM_CHARS else value[:MAX_ITEM_CHARS - 1] + "…"


def clean(data: Any) -> dict[str, Any] | None:
    """The model's notes in the stored shape, or None when they are not notes at all."""
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        out[field] = _clip(data.get(field)) if isinstance(data.get(field), str) else ""
    for field in LIST_FIELDS:
        items = data.get(field)
        if items is not None and not isinstance(items, list):
            return None
        out[field] = [_clip(i) for i in (items or []) if isinstance(i, str) and i.strip()][:MAX_ITEMS]
    return out


def _line(message: dict[str, Any]) -> str:
    from .conversation import assistant_text_of
    from .orchestrator.context import shortened_result

    role = message.get("role")
    if role == "user":
        extra = " [attached a picture]" if message.get("media") else ""
        return f"Person: {message.get('text') or ''}{extra}"
    if role == "assistant":
        parts = []
        said = assistant_text_of(message)
        if said:
            parts.append(f"Jarvis: {said}")
        for call in message.get("toolCalls") or []:
            parts.append(f"Jarvis used {call.get('name')} with {json.dumps(call.get('args') or {}, default=str)}")
        return "\n".join(parts)
    if role == "tool":
        return "\n".join(f"{r.get('name')} returned: {shortened_result(r.get('result'))}"
                         for r in message.get("toolResults") or [])
    return ""


def _prompt(previous: dict[str, Any] | None, fold: list[dict[str, Any]]) -> str:
    notes = json.dumps(previous or {}, ensure_ascii=False, indent=1)
    transcript = "\n".join(line for line in (_line(m) for m in fold) if line)
    return f"Notes so far:\n{notes}\n\nNext part of the conversation:\n{transcript}"


def _store(conversation_id: str, summary: dict[str, Any], covered_seq: int,
           previous_seq: int | None) -> bool:
    """Compare-and-set on what the summary covered before, so an Edit/Retry or another
    fold that landed meanwhile is never overwritten with stale notes."""
    db = get_db()
    if not db.execute("SELECT 1 FROM messages WHERE conversation_id = ? AND seq = ?",
                      (conversation_id, covered_seq)).fetchone():
        return False
    payload = json.dumps(summary, ensure_ascii=False)
    if previous_seq is None:
        cursor = db.execute(
            "INSERT OR IGNORE INTO conversation_summaries (conversation_id, summary, covered_seq, updated_at) "
            "VALUES (?, ?, ?, ?)", (conversation_id, payload, covered_seq, now_iso()))
    else:
        cursor = db.execute(
            "UPDATE conversation_summaries SET summary = ?, covered_seq = ?, updated_at = ? "
            "WHERE conversation_id = ? AND covered_seq = ?",
            (payload, covered_seq, now_iso(), conversation_id, previous_seq))
    return cursor.rowcount == 1


def refresh(conversation_id: str, budget_tokens: int | None) -> bool:
    """Fold what needs folding now. True when new notes were stored. Synchronous —
    `on_reply` is what runs it in the background."""
    from . import ai

    existing = get(conversation_id)
    covered = existing["coveredSeq"] if existing else 0
    fold = plan_fold(chat_store.get_messages_since(conversation_id, covered), budget_tokens)
    if not fold:
        return False
    try:
        answer = ai.ask(_prompt(existing["summary"] if existing else None, fold), system=SYSTEM,
                        data_class="personal", task_class="summarize", want_json=True, schema=SCHEMA,
                        background=True)
    except ai.NoModelAvailable as err:
        logger.info("no summary for %s: %s", conversation_id, err)
        return False
    summary = clean(answer.data)
    if summary is None:
        logger.warning("the notes for %s came back unusable; nothing stored", conversation_id)
        return False
    return _store(conversation_id, summary, fold[-1]["seq"], covered if existing else None)


def on_reply(event: Any) -> None:
    """`ASSISTANT_RESPONSE` observer: fold in the background if this conversation needs it."""
    if not is_enabled():
        return
    session_id = (event.payload or {}).get("sessionId")
    if not session_id or not chat_store.is_conversation(session_id):
        return
    budget = (event.payload or {}).get("contextBudget")
    budget = budget if isinstance(budget, int) else None
    with _lock:
        if session_id in _in_flight:
            return
        _in_flight.add(session_id)

    def work() -> None:
        try:
            refresh(session_id, budget)
        finally:
            with _lock:
                _in_flight.discard(session_id)

    background.run_in_background(work, name="conversation-summary")
