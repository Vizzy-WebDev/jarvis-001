"""Chat History persistence — every conversation's full transcript, on disk.

A port of server/chat-store.js. Built on db.py's SQLite connection; nothing else
in the project touches SQLite directly for this data.

A "conversation" here is the persisted record; the in-memory session is the
trimmed working copy the model actually sees (capped at 60 messages) — this store
keeps everything, forever, so nothing is lost to that cap.

Leaf module: imports only db.py and jscompat.py, both leaves. Safe for anything
under tools/ to reach transitively without tripping the loader/runner
circular-import invariant the root CLAUDE.md describes.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import get_db
from .jscompat import base36, compact_json, now_iso, now_ms, random_suffix

TITLE_MAX = 60
ACTIVE_KEY = "active_conversation_id"


def _make_id() -> str:
    return f"c{base36(now_ms())}{random_suffix(6)}"


def _title_from_text(text: str | None) -> str:
    trimmed = " ".join(str(text or "").split())
    if not trimmed:
        return "New chat"
    # The ellipsis is a single character, so the cut is at TITLE_MAX - 1.
    return trimmed if len(trimmed) <= TITLE_MAX else f"{trimmed[:TITLE_MAX - 1]}…"


def _row_to_conversation(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    out = {
        "id": row["id"],
        "title": row["title"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "pinned": bool(row["pinned"]),
        "archived": bool(row["archived"]),
    }
    # Mirrors `messageCount: row.message_count ?? undefined` — the key is absent
    # entirely, not null, when the query did not compute a count. JSON.stringify
    # drops an undefined value, so emitting null here would add a field the Node
    # response never had.
    if "message_count" in keys and row["message_count"] is not None:
        out["messageCount"] = row["message_count"]
    return out


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    """Decode one messages row back into a neutral message."""
    extra = json.loads(row["payload"]) if row["payload"] else {}
    # createdAt comes from this row's own created_at column, not the JSON
    # payload — the payload deliberately does not store the same timestamp twice.
    out: dict[str, Any] = {"id": f"m{row['id']}", "role": row["role"]}
    if row["text"] is not None:
        out["text"] = row["text"]
    out["createdAt"] = row["created_at"]
    out.update(extra)
    return out


# --- FTS5 query construction -------------------------------------------------


def _fts_phrase(q: str) -> str:
    """Quote the whole query as one literal phrase.

    FTS5 MATCH parses its input as query syntax, so a raw "C++ setup?" throws a
    syntax error. Quoting makes it a literal phrase rather than an operator
    sequence. This is the right shape for a human typing into a search box and
    expecting a literal match.
    """
    escaped = q.replace('"', '""')
    return f'"{escaped}"'


def _quoted_terms(q: str) -> list[str]:
    return [f'"{t.replace(chr(34), chr(34) * 2)}"' for t in str(q).strip().split() if t]


def _fts_and_terms(q: str) -> str:
    """Match messages containing ALL of q's words, in any order or position.

    Unlike _fts_phrase, this does not require adjacency. For a caller handing
    over a few keywords rather than a literal phrase: a whole-string phrase quote
    returns zero hits for a multi-keyword query whenever the words are not
    adjacent in that exact order, which is the common case, not the exception.
    Each word is quoted individually so a token like "C++", an apostrophe, or the
    literal word "NOT" can never be misread as query syntax; a plain space
    between already-literal tokens is AND to FTS5.
    """
    return " ".join(_quoted_terms(q))


def _fts_or_terms(q: str) -> str:
    """The same injection-safe per-term quoting, OR'd instead of AND'd.

    bm25's ranking still puts a message matching more terms above one matching
    fewer, so this widens what can match without losing precision. Exists for the
    fallback below: a long keyword reduction of a whole question routinely
    includes words never actually in the original message ("switching" for a
    message that said "switched"), and requiring all of them to match literally
    produces a false negative on exactly the queries the fallback exists to save.
    """
    return " OR ".join(_quoted_terms(q))


# --- reads -------------------------------------------------------------------


def list_conversations(query: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
    """All conversations, newest-updated first, pinned always ahead of unpinned.

    `query`, if given, full-text-searches message bodies as well as matching
    conversation titles — either match surfaces the conversation.
    """
    db = get_db()
    q = str(query or "").strip()
    archived_clause = "1=1" if include_archived else "c.archived = 0"
    count_expr = "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count"

    if q:
        rows = db.execute(
            f"""SELECT c.*, {count_expr}
                FROM conversations c
                WHERE ({archived_clause})
                  AND (
                    c.title LIKE ?
                    OR c.id IN (
                      SELECT m.conversation_id FROM messages m
                      JOIN messages_fts f ON f.rowid = m.id
                      WHERE f.text MATCH ?
                    )
                  )
                ORDER BY c.pinned DESC, c.updated_at DESC""",
            (f"%{q}%", _fts_phrase(q)),
        ).fetchall()
    else:
        rows = db.execute(
            f"""SELECT c.*, {count_expr}
                FROM conversations c
                WHERE ({archived_clause})
                ORDER BY c.pinned DESC, c.updated_at DESC"""
        ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return _row_to_conversation(row) if row else None


def is_conversation(conversation_id: str) -> bool:
    return bool(get_db().execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone())


def get_messages(conversation_id: str) -> list[dict[str, Any]]:
    """Every message in a conversation, oldest first."""
    rows = get_db().execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq ASC", (conversation_id,)
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def get_last_user_message_at(conversation_id: str) -> str | None:
    """Just the timestamp of the most recent USER message — a single indexed
    lookup, not a full transcript read. Used to gauge how recently the user was
    actually active without paying for get_messages()'s whole-conversation cost
    on every check."""
    row = get_db().execute(
        "SELECT created_at FROM messages WHERE conversation_id = ? AND role = 'user' "
        "ORDER BY seq DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return row["created_at"] if row else None


def get_messages_since(conversation_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
    """Messages strictly after `after_seq`, each carrying its own `seq` — so a
    memory checkpoint only ever analyses NEW content, never the whole transcript
    again."""
    rows = get_db().execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND seq > ? ORDER BY seq ASC",
        (conversation_id, after_seq),
    ).fetchall()
    return [{**_row_to_message(r), "seq": r["seq"]} for r in rows]


def _run_message_search(
    fts_query: str, limit: int, exclude_conversation_id: str | None, include_archived: bool
) -> list[dict[str, Any]]:
    params: list[Any] = [fts_query]
    exclude_clause = ""
    if exclude_conversation_id:
        exclude_clause = "AND c.id != ?"
        params.append(exclude_conversation_id)
    params.append(limit)
    archived_clause = "1=1" if include_archived else "c.archived = 0"

    rows = get_db().execute(
        f"""SELECT m.id, m.conversation_id, m.role, m.created_at, c.title,
                   snippet(messages_fts, 0, '', '', '…', 16) AS excerpt,
                   bm25(messages_fts) AS rank
            FROM messages m
            JOIN messages_fts f ON f.rowid = m.id
            JOIN conversations c ON c.id = m.conversation_id
            WHERE f.text MATCH ?
              AND ({archived_clause})
              {exclude_clause}
            ORDER BY rank
            LIMIT ?""",
        params,
    ).fetchall()

    return [
        {
            "conversationId": r["conversation_id"],
            "title": r["title"],
            "role": r["role"],
            "createdAt": r["created_at"],
            "excerpt": r["excerpt"],
        }
        for r in rows
    ]


def search_messages(
    query: str,
    limit: int = 8,
    exclude_conversation_id: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Full-text search over EVERY conversation's messages, ranked by bm25.

    `exclude_conversation_id` leaves out the conversation the caller is already
    in — its content is already in the model's context window, so surfacing it
    again would waste tokens and read as a non sequitur.
    """
    q = str(query or "").strip()
    if not q:
        return []
    and_query = _fts_and_terms(q)
    if not and_query:
        return []

    precise = _run_message_search(and_query, limit, exclude_conversation_id, include_archived)
    if precise:
        return precise
    # Nothing matched every term literally. Widening to OR only when the strict
    # pass came back empty keeps a well-targeted query at its original precision.
    return _run_message_search(_fts_or_terms(q), limit, exclude_conversation_id, include_archived)


# --- writes ------------------------------------------------------------------


def create_conversation() -> dict[str, Any] | None:
    db = get_db()
    conversation_id = _make_id()
    ts = now_iso()
    db.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at, pinned, archived) "
        "VALUES (?, ?, ?, ?, 0, 0)",
        (conversation_id, "New chat", ts, ts),
    )
    return get_conversation(conversation_id)


def append_message(conversation_id: str, message: dict[str, Any]) -> dict[str, Any] | None:
    """Append one neutral message. Everything but role/text rides in `payload`.

    Renames the conversation from its "New chat" placeholder the first time a
    user message with text arrives, so titles need no separate model call. The
    insert and the conversation's updated_at (and title) touch happen in one
    transaction, so a crash mid-write can never leave the index pointing at a
    conversation whose row count disagrees with what is really on disk.
    """
    db = get_db()
    rest = {k: v for k, v in message.items() if k not in ("role", "text")}
    role = message.get("role")
    text = message.get("text")
    # Mirrors `Object.values(rest).some(v => v !== undefined)`: a payload of
    # nothing-but-undefined is stored as NULL, not as "{}".
    has_extra = any(v is not None for v in rest.values())
    payload = compact_json(rest) if has_extra else None
    ts = now_iso()

    db.execute("BEGIN")
    try:
        row = db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        next_seq = (row["seq"] if row else 0) + 1

        db.execute(
            "INSERT INTO messages (conversation_id, seq, role, text, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, next_seq, role, text if text is not None else None, payload, ts),
        )

        conv = db.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        should_title = bool(conv and conv["title"] == "New chat" and role == "user" and text)
        if should_title:
            db.execute(
                "UPDATE conversations SET updated_at = ?, title = ? WHERE id = ?",
                (ts, _title_from_text(text), conversation_id),
            )
        else:
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conversation_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    return get_conversation(conversation_id)


def remove_last_message_if_matches(conversation_id: str, role: str) -> bool:
    """Delete the most recent message row, but only if its role matches.

    A no-op otherwise, so a caller can call this unconditionally without a
    separate read-then-check. Keeps this persisted copy in sync when an in-flight
    turn dies between writing a tool call and writing its result.
    """
    db = get_db()
    row = db.execute(
        "SELECT id, role FROM messages WHERE conversation_id = ? ORDER BY seq DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if not row or row["role"] != role:
        return False
    db.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
    return True


def update_last_assistant_message(conversation_id: str, patch: dict[str, Any]) -> bool:
    """Merge `patch` into the payload of the most recent assistant message.

    The one place chat history is ever edited after the fact, and only for
    interrupt correctness. The FULL generated text stays in `text` — nothing is
    deleted, since the user might still want to see what the model would have
    said — while `spokenText` in the payload is what the next turn's history
    actually sends: the model should believe it said only what was truly heard,
    not everything it happened to finish generating after being cut off.
    """
    db = get_db()
    row = db.execute(
        "SELECT id, payload FROM messages WHERE conversation_id = ? AND role = 'assistant' "
        "ORDER BY seq DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if not row:
        return False
    existing = json.loads(row["payload"]) if row["payload"] else {}
    merged = {**existing, **patch}
    db.execute("UPDATE messages SET payload = ? WHERE id = ?", (compact_json(merged), row["id"]))
    return True


def rename_conversation(conversation_id: str, title: str) -> dict[str, Any] | None:
    # Deliberately does not touch updated_at — renaming should not bump a
    # conversation to the top of the recency sort the way actually talking in it
    # does.
    trimmed = str(title or "").strip()
    if not trimmed:
        raise ValueError("A conversation needs a title.")
    get_db().execute(
        "UPDATE conversations SET title = ? WHERE id = ?", (trimmed[:200], conversation_id)
    )
    return get_conversation(conversation_id)


def set_pinned(conversation_id: str, pinned: bool) -> dict[str, Any] | None:
    get_db().execute(
        "UPDATE conversations SET pinned = ? WHERE id = ?", (1 if pinned else 0, conversation_id)
    )
    return get_conversation(conversation_id)


def set_archived(conversation_id: str, archived: bool) -> dict[str, Any] | None:
    get_db().execute(
        "UPDATE conversations SET archived = ? WHERE id = ?", (1 if archived else 0, conversation_id)
    )
    return get_conversation(conversation_id)


def delete_conversation(conversation_id: str) -> None:
    """Deletes a conversation and, via ON DELETE CASCADE, all of its messages."""
    get_db().execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def get_active_id() -> str | None:
    row = get_db().execute("SELECT value FROM app_state WHERE key = ?", (ACTIVE_KEY,)).fetchone()
    return (row["value"] if row else None) or None


def set_active_id(conversation_id: str) -> None:
    get_db().execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (ACTIVE_KEY, conversation_id),
    )
