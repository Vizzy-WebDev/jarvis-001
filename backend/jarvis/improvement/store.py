"""Outcomes, lessons, proposals, rules, changes — and the spend ledger.

The chain is deliberately four separate things: an OUTCOME is something that
happened, a LESSON is an observation about several of them, a PROPOSAL is a
change worth making, and a RULE is a change that was made. Collapsing any two
loses the property that matters — that a single bad turn cannot become a
permanent rule.

The budget lives in `app_state` rather than a new table: two small singleton
counters are exactly what that key/value table exists for. Without both a budget
and a cadence floor, a fifteen-minute background tick checking "are there five
unreviewed outcomes?" fires on nearly every tick of a busy day.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import get_db
from ..jscompat import now_iso

DAILY_BUDGET = 2      # reflect + synthesize, combined
WEEKLY_BUDGET = 4     # outside research + life patterns, combined


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json(value: Any) -> str | None:
    return json.dumps(value, default=str) if value is not None else None


def _parse(value: Any, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


# --- outcomes ----------------------------------------------------------------

def record_outcome(*, source: str, title: str, goal: str | None = None,
                   kind: str | None = None, status: str, source_ref: str | None = None,
                   entity_ref: str | None = None, retries: int = 0,
                   error: str | None = None, tool_summary: Any = None,
                   escalations: int = 0) -> dict[str, Any] | None:
    """Write down that something happened. No model call, ever.

    Idempotent on (source, source_ref): a job's terminal status can genuinely be
    emitted twice, and that must never become two outcomes. A NULL source_ref is
    distinct every time, which is right for a correction — each one is a new
    event, not a repeat.
    """
    outcome_id = _id("out")
    try:
        get_db().execute(
            "INSERT INTO improvement_outcomes (id, source, source_ref, entity_ref, title, "
            "goal, kind, status, retries, error, tool_summary, escalations, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (outcome_id, source, source_ref, entity_ref, title, goal, kind, status,
             retries, error, _json(tool_summary), escalations, now_iso()))
    except Exception:  # noqa: BLE001 — a duplicate is the expected case, not an error
        return None
    return get_outcome(outcome_id)


def get_outcome(outcome_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM improvement_outcomes WHERE id = ?",
                           (outcome_id,)).fetchone()
    return dict(row) if row else None


def list_unreviewed_outcomes(limit: int = 40) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM improvement_outcomes WHERE reviewed_at IS NULL "
        "ORDER BY created_at LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_unreviewed_outcomes() -> int:
    return int(get_db().execute(
        "SELECT COUNT(*) AS n FROM improvement_outcomes WHERE reviewed_at IS NULL"
    ).fetchone()["n"])


def mark_outcomes_reviewed(ids: list[str]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    get_db().execute(
        f"UPDATE improvement_outcomes SET reviewed_at = ? WHERE id IN ({placeholders})",
        [now_iso(), *ids])


# --- lessons -----------------------------------------------------------------

def create_lesson(*, text: str, scope: str = "general", kind: str = "lesson",
                  evidence: list[str] | None = None, confidence: float | None = None,
                  source_tier: int = 1, source_url: str | None = None) -> dict[str, Any]:
    lesson_id = _id("les")
    get_db().execute(
        "INSERT INTO improvement_lessons (id, kind, text, scope, evidence, confidence, "
        "source_tier, source_url, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (lesson_id, kind, text, scope, _json(evidence or []), confidence, source_tier,
         source_url, now_iso(), now_iso()))
    return get_lesson(lesson_id)  # type: ignore[return-value]


def get_lesson(lesson_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM improvement_lessons WHERE id = ?",
                           (lesson_id,)).fetchone()
    if row is None:
        return None
    lesson = dict(row)
    lesson["evidence"] = _parse(lesson.get("evidence"), [])
    return lesson


def list_lessons(status: str | None = "active", scope: str | None = None) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = get_db().execute(
        f"SELECT * FROM improvement_lessons {where} ORDER BY created_at DESC", params).fetchall()
    out = []
    for row in rows:
        lesson = dict(row)
        lesson["evidence"] = _parse(lesson.get("evidence"), [])
        out.append(lesson)
    return out


def delete_lesson(lesson_id: str) -> None:
    """Permanent, and reachable only from the archived view — the same
    archive-then-delete discipline rules and memories both keep."""
    get_db().execute("DELETE FROM improvement_lessons WHERE id = ?", (lesson_id,))


def delete_proposal(proposal_id: str) -> None:
    get_db().execute("DELETE FROM improvement_proposals WHERE id = ?", (proposal_id,))


def set_lesson_status(lesson_id: str, status: str) -> None:
    get_db().execute(
        "UPDATE improvement_lessons SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_iso(), lesson_id))


# --- proposals ---------------------------------------------------------------

def create_proposal(*, kind: str, title: str, rationale: str = "",
                    payload: Any = None, evidence: list[str] | None = None,
                    source_tier: int = 1, source_url: str | None = None,
                    conflict_with: str | None = None,
                    helps_jarvis: str | None = None,
                    helps_user: str | None = None) -> dict[str, Any]:
    proposal_id = _id("prop")
    get_db().execute(
        "INSERT INTO improvement_proposals (id, kind, title, rationale, helps_jarvis, "
        "helps_user, payload, evidence, source_tier, source_url, status, conflict_with, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (proposal_id, kind, title, rationale, helps_jarvis, helps_user, _json(payload),
         _json(evidence or []), source_tier, source_url, conflict_with, now_iso()))
    return get_proposal(proposal_id)  # type: ignore[return-value]


def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM improvement_proposals WHERE id = ?",
                           (proposal_id,)).fetchone()
    if row is None:
        return None
    proposal = dict(row)
    proposal["payload"] = _parse(proposal.get("payload"))
    proposal["evidence"] = _parse(proposal.get("evidence"), [])
    # camelCase alongside the column names, so the pure policy can read a
    # proposal straight from the store without a translation step in between.
    proposal["sourceTier"] = proposal.get("source_tier")
    proposal["conflictWith"] = proposal.get("conflict_with")
    return proposal


def list_proposals(status: str | None = "pending") -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT id FROM improvement_proposals WHERE (? IS NULL OR status = ?) "
        "ORDER BY created_at DESC", (status, status)).fetchall()
    return [p for p in (get_proposal(r["id"]) for r in rows) if p]


def set_proposal_status(proposal_id: str, status: str) -> None:
    get_db().execute(
        "UPDATE improvement_proposals SET status = ?, resolved_at = ? WHERE id = ?",
        (status, now_iso(), proposal_id))


def set_proposal_implementation(proposal_id: str, *, prompt: str, target: str) -> None:
    get_db().execute(
        "UPDATE improvement_proposals SET implementation_prompt = ?, "
        "implementation_target = ? WHERE id = ?", (prompt, target, proposal_id))


# --- rules -------------------------------------------------------------------

def create_rule(*, text: str, scope: str = "general",
                source_proposal_id: str | None = None) -> dict[str, Any]:
    rule_id = _id("rule")
    get_db().execute(
        "INSERT INTO improvement_rules (id, text, scope, active, source_proposal_id, "
        "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
        (rule_id, text, scope, source_proposal_id, now_iso(), now_iso()))
    return get_rule(rule_id)  # type: ignore[return-value]


def get_rule(rule_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM improvement_rules WHERE id = ?", (rule_id,)).fetchone()
    return dict(row) if row else None


def list_rules(*, active_only: bool = False, scope: str | None = None,
               include_archived: bool = False) -> list[dict[str, Any]]:
    clauses, params = [], []
    if active_only:
        clauses.append("active = 1")
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = get_db().execute(
        f"SELECT * FROM improvement_rules {where} ORDER BY created_at DESC", params).fetchall()
    return [dict(r) for r in rows]


def set_rule_active(rule_id: str, active: bool) -> None:
    get_db().execute("UPDATE improvement_rules SET active = ?, updated_at = ? WHERE id = ?",
                     (1 if active else 0, now_iso(), rule_id))


def update_rule_text(rule_id: str, text: str) -> None:
    get_db().execute("UPDATE improvement_rules SET text = ?, updated_at = ? WHERE id = ?",
                     (text, now_iso(), rule_id))


def archive_rule(rule_id: str) -> None:
    get_db().execute("UPDATE improvement_rules SET archived_at = ? WHERE id = ?",
                     (now_iso(), rule_id))


def restore_rule(rule_id: str) -> None:
    get_db().execute("UPDATE improvement_rules SET archived_at = NULL, updated_at = ? "
                     "WHERE id = ?", (now_iso(), rule_id))


def delete_rule(rule_id: str) -> None:
    """Permanent, and only ever reachable from an archived row.

    A change row's undo needs the rule it points at to still exist, so a hard
    delete has to go through archive first — which is why nothing offers this
    from the live list.
    """
    get_db().execute("DELETE FROM improvement_rules WHERE id = ?", (rule_id,))


def active_rules_text(scope: str = "general") -> str:
    """The live rules, ready for the system prompt. Empty string when there are
    none, so a caller never injects a heading with nothing under it."""
    rules = list_rules(active_only=True, scope=scope)
    return "\n".join(f"- {r['text']}" for r in rules)


# --- the change log ----------------------------------------------------------

def record_change(*, kind: str, target: str, before: Any, after: Any,
                  reason: str | None = None, proposal_id: str | None = None) -> dict[str, Any]:
    """Append-only: what changed, what it was, what it became. `before`/`after`
    are what makes undo possible AND checkable — undo compares the live value
    against `after` before restoring `before`."""
    # The change log's id is an autoincrementing integer, unlike every other id
    # here — it is an append-only sequence rather than something referred to by
    # name, and SQLite assigns it.
    cursor = get_db().execute(
        "INSERT INTO improvement_changes (kind, target, before, after, reason, "
        "proposal_id, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, target, _json(before), _json(after), reason, proposal_id, now_iso()))
    return get_change(int(cursor.lastrowid or 0))  # type: ignore[return-value]


def get_change(change_id: int | str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM improvement_changes WHERE id = ?",
                           (change_id,)).fetchone()
    if row is None:
        return None
    change = dict(row)
    change["before"] = _parse(change.get("before"))
    change["after"] = _parse(change.get("after"))
    return change


def list_changes(limit: int = 50) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT id FROM improvement_changes ORDER BY applied_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [c for c in (get_change(r["id"]) for r in rows) if c]


def mark_change_undone(change_id: int | str) -> None:
    get_db().execute("UPDATE improvement_changes SET undone_at = ? WHERE id = ?",
                     (now_iso(), change_id))


# --- the spend ledger --------------------------------------------------------

def _read_state(key: str, fallback: Any) -> Any:
    row = get_db().execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return _parse(row["value"], fallback) if row else fallback


def _write_state(key: str, value: Any) -> None:
    get_db().execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, _json(value)))


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def daily_budget_remaining() -> int:
    state = _read_state("improvement_daily_budget", {"day": _today(), "used": 0})
    if state.get("day") != _today():
        return DAILY_BUDGET
    return max(0, DAILY_BUDGET - int(state.get("used") or 0))


def try_consume_daily_budget() -> bool:
    today = _today()
    state = _read_state("improvement_daily_budget", {"day": today, "used": 0})
    used = int(state.get("used") or 0) if state.get("day") == today else 0
    if used >= DAILY_BUDGET:
        return False
    _write_state("improvement_daily_budget", {"day": today, "used": used + 1})
    return True


def _week(state: Any) -> dict[str, Any]:
    """A rolling seven days from first use, not a calendar week: "how many calls
    in the last week" is the thing actually being budgeted."""
    now = datetime.now(timezone.utc)
    if not isinstance(state, dict) or not state.get("weekStart"):
        return {"weekStart": now.isoformat(), "used": 0}
    try:
        started = datetime.fromisoformat(str(state["weekStart"]))
    except ValueError:
        return {"weekStart": now.isoformat(), "used": 0}
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if now - started > timedelta(days=7):
        return {"weekStart": now.isoformat(), "used": 0}
    return state


def weekly_budget_remaining() -> int:
    state = _week(_read_state("improvement_weekly_budget", None))
    return max(0, WEEKLY_BUDGET - int(state.get("used") or 0))


def try_consume_weekly_budget() -> bool:
    state = _week(_read_state("improvement_weekly_budget", None))
    if int(state.get("used") or 0) >= WEEKLY_BUDGET:
        return False
    _write_state("improvement_weekly_budget",
                 {"weekStart": state["weekStart"], "used": int(state.get("used") or 0) + 1})
    return True


def get_last_run_at(key: str) -> str | None:
    return _read_state(f"improvement_last_{key}", None)


def set_last_run_at(key: str) -> None:
    _write_state(f"improvement_last_{key}", now_iso())
