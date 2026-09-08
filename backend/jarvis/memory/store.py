"""Durable facts about the user, and the drafts waiting to become them.

Two tables that never cascade into each other, enforced by the SCHEMA rather
than by remembering: `memory_candidates.conversation_id` cascades, so an
unreviewed draft dies with the conversation it came from; `memories` has no
foreign key to conversations at all, so an approved memory cannot be cascaded
away by a conversation delete.

Recalling a durable FACT is not a search: the approved set is small enough to sit
in the prompt, and `approved_memories_text()` is capped so that stays true as the
store grows rather than being an assumption that quietly stops holding. Recalling
something SAID is a different problem and does use search — `search_conversations`.

**Added in the port (§22): `importance` and `expires_at`.** Importance is NULL
until something actually judges it; a default of 3 would be a number nobody chose,
read later as though someone had. An expiring memory is making a specific,
time-bounded claim, and once it lapses it stops being asserted rather than quietly
ageing into a lie.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from ..db import get_db
from ..jscompat import now_iso

MAX_INJECTED_MEMORIES = 30
MAX_INJECTED_CHARS = 6000

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _row_to_memory(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "category": row["category"],
        "text": row["text"],
        "sourceKind": row["source_kind"],
        "sourceRef": row["source_ref"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "archived": bool(row["archived"]),
        "confidence": row["confidence"],
        # HOW consent was given — 'approved', 'auto', 'explicit', 'legacy'.
        # Distinct from sourceKind, which records where the content came from.
        "origin": row["origin"],
        "importance": row["importance"],
        "expiresAt": row["expires_at"],
    }


def _row_to_candidate(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversationId": row["conversation_id"],
        "sourceKind": row["source_kind"],
        "sourceRef": row["source_ref"],
        "category": row["category"],
        "text": row["text"],
        "conflictWith": row["conflict_with"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "resolvedAt": row["resolved_at"],
        "confidence": row["confidence"],
    }


# --- memories ----------------------------------------------------------------

def list_memories(category: str | None = None, *, include_archived: bool = False,
                  query: str | None = None, origin: str | None = None,
                  include_expired: bool = False) -> list[dict[str, Any]]:
    clauses, params = [], []
    if not include_archived:
        clauses.append("archived = 0")
    if not include_expired:
        # An expired memory is not deleted — the user may want to see it, and
        # deleting on read would be a side effect in a query. It is simply not
        # returned to anything that would treat it as currently true.
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(now_iso())
    if category:
        clauses.append("category = ?")
        params.append(category)
    if origin:
        clauses.append("origin = ?")
        params.append(origin)
    if query and query.strip():
        clauses.append("text LIKE ?")
        params.append(f"%{query.strip()}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = get_db().execute(
        f"SELECT * FROM memories {where} ORDER BY updated_at DESC", params).fetchall()
    return [_row_to_memory(r) for r in rows]


def get_memory(memory_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return _row_to_memory(row) if row else None


def conflicted_memory_ids() -> set[str]:
    """Memories a still-pending candidate says are contradicted.

    The other half of "a conflict always needs a human": while it waits, the OLD,
    possibly-wrong memory must stop being asserted to the model as settled fact
    every turn. Confirmed live in the original — a memory reading "uses a Mac"
    kept being injected while a pending candidate said the opposite.
    """
    rows = get_db().execute(
        "SELECT DISTINCT conflict_with AS id FROM memory_candidates "
        "WHERE status = 'pending' AND conflict_with IS NOT NULL").fetchall()
    return {r["id"] for r in rows if r["id"]}


def _short_date(iso: str | None) -> str | None:
    """"Aug 12, 2026" — absolute, not relative, so it stays true however long the
    memory sits in a prompt before the model reads it."""
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"{_MONTHS[parsed.month - 1]} {parsed.day}, {parsed.year}"


def approved_memories_text(memories: Iterable[dict[str, Any]] | None = None) -> str:
    """Approved memories, grouped by category and dated, ready for a prompt.

    Dated for the same reason a searched conversation result is: an old note is
    not automatically still true. Capped so "the set is small enough to just be
    in context" stays a fact rather than an assumption.
    """
    if memories is None:
        conflicted = conflicted_memory_ids()
        memories = [m for m in list_memories() if m["id"] not in conflicted]

    by_category: dict[str, list[str]] = {}
    used = count = 0
    for memory in memories:
        if count >= MAX_INJECTED_MEMORIES:
            break
        date = _short_date(memory.get("updatedAt") or memory.get("createdAt"))
        line = f"- {memory['text']}" + (f" (noted {date})" if date else "")
        if used + len(line) > MAX_INJECTED_CHARS:
            break
        by_category.setdefault(memory["category"], []).append(line)
        used += len(line)
        count += 1

    lines: list[str] = []
    for category, entries in by_category.items():
        lines.append(f"{category}:")
        lines.extend(entries)
    return "\n".join(lines)


def create_memory(*, category: str, text: str, source_kind: str | None = None,
                  source_ref: str | None = None, confidence: float | None = None,
                  origin: str = "approved", importance: int | None = None,
                  expires_at: str | None = None) -> dict[str, Any]:
    """Create a memory directly, bypassing the approval queue.

    Only for callers where the user has ALREADY explicitly consented — an
    approved candidate, or a "remember that..." the user just confirmed.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("A memory needs some text.")
    name = (category or "Uncategorized").strip() or "Uncategorized"
    db = get_db()
    memory_id = _new_id("mem")
    stamp = now_iso()
    db.execute("BEGIN")
    try:
        db.execute(
            "INSERT INTO memories (id, category, text, source_kind, source_ref, created_at, "
            "updated_at, archived, confidence, origin, importance, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (memory_id, name, body, source_kind, source_ref, stamp, stamp,
             confidence, origin, importance, expires_at))
        # An initial version row, so history reads "created" from the start
        # rather than beginning at the first edit.
        db.execute(
            "INSERT INTO memory_versions (memory_id, text, category, changed_at, reason) "
            "VALUES (?, ?, ?, ?, ?)", (memory_id, body, name, stamp, "Created."))
        _ensure_category(name)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return get_memory(memory_id)  # type: ignore[return-value]


def update_memory(memory_id: str, *, text: str | None = None, category: str | None = None,
                  importance: int | None = None, expires_at: str | None = None,
                  reason: str = "Edited.", origin: str | None = None) -> dict[str, Any]:
    """Edit a memory, keeping the PAST state as a version row.

    The version is written before the overwrite, so "why did this change?" has a
    real answer months later instead of only the current text.
    """
    existing = get_memory(memory_id)
    if existing is None:
        raise KeyError(f"No such memory: {memory_id}")

    db = get_db()
    stamp = now_iso()
    new_text = (text if text is not None else existing["text"]).strip()
    new_category = (category if category is not None else existing["category"]).strip()
    if not new_text:
        raise ValueError("A memory needs some text.")

    db.execute("BEGIN")
    try:
        db.execute(
            "INSERT INTO memory_versions (memory_id, text, category, changed_at, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (memory_id, existing["text"], existing["category"], stamp, reason))
        db.execute(
            "UPDATE memories SET text = ?, category = ?, importance = ?, expires_at = ?, "
            "updated_at = ?, origin = COALESCE(?, origin) WHERE id = ?",
            (new_text, new_category,
             importance if importance is not None else existing["importance"],
             expires_at if expires_at is not None else existing["expiresAt"],
             stamp, origin, memory_id))
        _ensure_category(new_category)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return get_memory(memory_id)  # type: ignore[return-value]


def archive_memory(memory_id: str) -> None:
    get_db().execute("UPDATE memories SET archived = 1, updated_at = ? WHERE id = ?",
                     (now_iso(), memory_id))


def restore_memory(memory_id: str) -> None:
    get_db().execute("UPDATE memories SET archived = 0, updated_at = ? WHERE id = ?",
                     (now_iso(), memory_id))


def delete_memory(memory_id: str) -> None:
    get_db().execute("DELETE FROM memories WHERE id = ?", (memory_id,))


def version_history(memory_id: str) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? ORDER BY id DESC",
        (memory_id,)).fetchall()
    return [{"id": r["id"], "text": r["text"], "category": r["category"],
             "changedAt": r["changed_at"], "reason": r["reason"]} for r in rows]


def merge_memories(primary_id: str, other_ids: list[str], merged_text: str,
                   merged_category: str | None = None) -> dict[str, Any]:
    merged = update_memory(primary_id, text=merged_text, category=merged_category,
                           reason="Merged with duplicates.")
    for other in other_ids:
        if other != primary_id:
            archive_memory(other)
    return merged


# --- categories --------------------------------------------------------------

def _ensure_category(name: str) -> None:
    get_db().execute(
        "INSERT OR IGNORE INTO memory_categories (name, status) VALUES (?, 'approved')",
        (name,))


def list_categories(include_pending: bool = True) -> list[dict[str, Any]]:
    where = "" if include_pending else "WHERE status = 'approved'"
    rows = get_db().execute(f"SELECT * FROM memory_categories {where} ORDER BY name").fetchall()
    return [{"name": r["name"], "status": r["status"]} for r in rows]


def propose_category(name: str) -> None:
    """A category's existence is itself approval-gated: a model proposing one
    inserts it as pending, and only the user turns it approved."""
    get_db().execute(
        "INSERT OR IGNORE INTO memory_categories (name, status) VALUES (?, 'pending')",
        ((name or "").strip(),))


def approve_category(name: str) -> None:
    get_db().execute("UPDATE memory_categories SET status = 'approved' WHERE name = ?", (name,))


def merge_category_into(source: str, target: str) -> None:
    db = get_db()
    db.execute("BEGIN")
    try:
        db.execute("UPDATE memories SET category = ?, updated_at = ? WHERE category = ?",
                   (target, now_iso(), source))
        db.execute("UPDATE memory_candidates SET category = ? WHERE category = ?",
                   (target, source))
        db.execute("DELETE FROM memory_categories WHERE name = ?", (source,))
        _ensure_category(target)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


# --- candidates --------------------------------------------------------------

def list_pending_candidates() -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM memory_candidates WHERE status = 'pending' ORDER BY created_at").fetchall()
    return [_row_to_candidate(r) for r in rows]


def get_candidate(candidate_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM memory_candidates WHERE id = ?",
                           (candidate_id,)).fetchone()
    return _row_to_candidate(row) if row else None


def create_candidate(*, conversation_id: str | None = None, source_kind: str,
                     source_ref: str | None = None, category: str, text: str,
                     conflict_with: str | None = None,
                     confidence: float | None = None) -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise ValueError("A candidate needs some text.")
    candidate_id = _new_id("cand")
    get_db().execute(
        "INSERT INTO memory_candidates (id, conversation_id, source_kind, source_ref, "
        "category, text, conflict_with, status, created_at, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (candidate_id, conversation_id, source_kind, source_ref,
         (category or "Uncategorized").strip() or "Uncategorized",
         body, conflict_with, now_iso(), confidence))
    return get_candidate(candidate_id)  # type: ignore[return-value]


def _resolve_into_memory(candidate_id: str, edits: dict[str, Any], *, origin: str,
                         status: str) -> dict[str, Any]:
    candidate = get_candidate(candidate_id)
    if candidate is None:
        raise KeyError(f"No such candidate: {candidate_id}")
    memory = create_memory(
        category=edits.get("category") or candidate["category"],
        text=edits.get("text") or candidate["text"],
        source_kind=candidate["sourceKind"],
        source_ref=candidate["sourceRef"],
        confidence=candidate["confidence"],
        origin=origin,
        importance=edits.get("importance"),
        expires_at=edits.get("expiresAt"),
    )
    get_db().execute("UPDATE memory_candidates SET status = ?, resolved_at = ? WHERE id = ?",
                     (status, now_iso(), candidate_id))
    return memory


def approve_candidate(candidate_id: str, edits: dict[str, Any] | None = None) -> dict[str, Any]:
    return _resolve_into_memory(candidate_id, edits or {}, origin="approved", status="approved")


def auto_approve_candidate(candidate_id: str) -> dict[str, Any]:
    """Saved without being asked, because it cleared the trust threshold. The
    origin records that, so the Memory screen can show HOW it got here."""
    return _resolve_into_memory(candidate_id, {}, origin="auto", status="approved")


def reject_candidate(candidate_id: str) -> None:
    get_db().execute("UPDATE memory_candidates SET status = 'rejected', resolved_at = ? "
                     "WHERE id = ?", (now_iso(), candidate_id))


def resolve_conflict(candidate_id: str, choice: str,
                     edits: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """`choice`: 'keep-old' | 'use-new' | 'keep-both'.

    'use-new' EDITS the contradicted memory rather than creating a second one —
    two memories asserting opposite things is the state this exists to prevent.
    """
    candidate = get_candidate(candidate_id)
    if candidate is None:
        raise KeyError(f"No such candidate: {candidate_id}")
    edits = edits or {}

    if choice == "keep-old":
        reject_candidate(candidate_id)
        return None
    if choice == "use-new" and candidate["conflictWith"]:
        updated = update_memory(
            candidate["conflictWith"],
            text=edits.get("text") or candidate["text"],
            category=edits.get("category") or candidate["category"],
            reason="Replaced by a newer note.", origin="approved")
        get_db().execute("UPDATE memory_candidates SET status = 'approved', resolved_at = ? "
                         "WHERE id = ?", (now_iso(), candidate_id))
        return updated
    return approve_candidate(candidate_id, edits)


# --- checkpoints -------------------------------------------------------------

def get_checkpoint(conversation_id: str) -> int:
    """How far extraction has already looked, so a checkpoint only ever analyses
    NEW messages and never re-proposes the same candidate twice."""
    row = get_db().execute("SELECT last_seq FROM memory_checkpoints WHERE conversation_id = ?",
                           (conversation_id,)).fetchone()
    return int(row["last_seq"]) if row else 0


def set_checkpoint(conversation_id: str, last_seq: int) -> None:
    get_db().execute(
        "INSERT INTO memory_checkpoints (conversation_id, last_seq, checked_at) "
        "VALUES (?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
        "last_seq = excluded.last_seq, checked_at = excluded.checked_at",
        (conversation_id, int(last_seq), now_iso()))
