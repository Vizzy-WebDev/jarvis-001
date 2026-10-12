"""What a piece of background work is FOR, and where it has got to — kept outside the model.

A job used to be a goal string: whatever the conversation happened to pass along, with no
statement of what "done" means and nothing that survived a round except the transcript's own
tail. Work that outlasts one reply needs two things on record, and both live on the job's row:

- **The brief** (`jobs.brief`): the outcome wanted, what will be true when it is done
  (`doneWhen` — what a finished result is later checked against), anything the conversation
  settled (`context`), the person's own recent words (`said`, verbatim — never only a
  paraphrase of them), and what had already been found before the work was handed over
  (`already`). Written once, when the work is taken on.
- **The ledger** (`jobs.plan`): the plan as steps with a status each, what has been found so
  far, what is still open, and the last round's own progress note. The worker updates it
  (`update_plan`); the note is kept by code whether it does or not.

Both are put in front of the worker on EVERY round (`worker_sections`), in the system prompt,
so neither can scroll out of a long transcript or be lost when a restart replays a round. The
research this follows: long-running agents hold together through state kept outside the
context window — a task list and a progress record — far more than through a longer window.

A leaf over `job_store`: no model call, nothing imported from the turn loop.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..jscompat import now_iso
from . import job_store

STEP_STATUSES = ("todo", "doing", "done", "blocked", "dropped")

MAX_STEPS = 20
MAX_FOUND = 40
MAX_OPEN = 10
ITEM_CHARS = 300
CONTEXT_CHARS = 2000
SAID_CHARS = 600
ALREADY_CHARS = 4000
NOTE_CHARS = 1500
#: How many earlier rounds' notes are kept behind the latest one.
EARLIER_NOTES = 5

SESSION_PREFIX = "job:"

_THINK_CLOSED = re.compile(r"^\s*<think(?:ing)?>.*?</think(?:ing)?>\s*", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"^\s*<think(?:ing)?>.*?(?:\n\s*\n|\Z)\s*", re.DOTALL | re.IGNORECASE)


def clean(text: str) -> str:
    """A result or note without a model's leading thinking block. Found live: a gateway model
    opened its reply with `<think>…` and never closed the tag, and that reasoning was delivered
    as the result. A closed block is removed whole; an unclosed one, up to its first blank line
    (it is explicitly marked as thinking, and what is being kept is what follows it)."""
    text = text or ""
    cleaned = _THINK_CLOSED.sub("", text, count=1)
    if cleaned == text:
        cleaned = _THINK_OPEN.sub("", text, count=1)
    return cleaned.strip() or text.strip()


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _lines(values: Any, limit: int, each: int = ITEM_CHARS) -> list[str]:
    if isinstance(values, str):
        values = [values]
    out = []
    for value in values if isinstance(values, (list, tuple)) else []:
        text = _short(value, each)
        if text and text not in out:
            out.append(text)
    return out[:limit]


# --- the brief ----------------------------------------------------------------------------------

def make_brief(*, outcome: str, done_when: Any = None, context: str = "", said: Any = None,
               already: str = "") -> dict[str, Any]:
    """The brief of a new piece of work, normalised. `done_when` may be empty: a brief that
    cannot say what done looks like is still a brief, and it says so (`worker_sections`)."""
    return {
        "outcome": str(outcome or "").strip(),
        "doneWhen": _lines(done_when, 8),
        "context": str(context or "").strip()[:CONTEXT_CHARS],
        "said": _lines(said, 3, SAID_CHARS),
        "already": str(already or "").strip()[:ALREADY_CHARS],
    }


def brief_of(job: dict[str, Any] | None) -> dict[str, Any]:
    """A job's brief. A job made before briefs existed (or from the Jobs screen, with only a
    goal) has one made of its goal — the same record either way, so nothing reads two shapes."""
    job = job or {}
    brief = job.get("brief")
    if isinstance(brief, dict) and brief.get("outcome"):
        return {**make_brief(outcome=""), **brief}
    return make_brief(outcome=job.get("goal") or job.get("title") or "")


def opening_text(job: dict[str, Any]) -> str:
    """What the work is first told, and what each later round restates: the outcome and what
    done looks like. The rest of the brief rides in the prompt, where it cannot scroll away."""
    brief = brief_of(job)
    text = brief["outcome"]
    if brief["doneWhen"]:
        text += "\n\nIt is done when:\n" + "\n".join(f"- {line}" for line in brief["doneWhen"])
    return text


# --- the ledger ---------------------------------------------------------------------------------

def ledger_of(job: dict[str, Any] | None) -> dict[str, Any]:
    raw = (job or {}).get("plan")
    raw = raw if isinstance(raw, dict) else {}
    steps = []
    for entry in raw.get("steps") or []:
        if isinstance(entry, dict) and str(entry.get("text") or "").strip():
            status = entry.get("status") if entry.get("status") in STEP_STATUSES else "todo"
            steps.append({"text": _short(entry["text"], ITEM_CHARS), "status": status,
                          **({"note": _short(entry["note"], ITEM_CHARS)} if entry.get("note") else {})})
    return {"steps": steps[:MAX_STEPS], "found": _lines(raw.get("found"), MAX_FOUND),
            "open": _lines(raw.get("open"), MAX_OPEN),
            "note": str(raw.get("note") or "")[:NOTE_CHARS],
            "earlier": [str(n)[:NOTE_CHARS] for n in (raw.get("earlier") or [])][-EARLIER_NOTES:],
            "blocked": raw.get("blocked") if isinstance(raw.get("blocked"), dict) else None,
            "updatedAt": raw.get("updatedAt")}


def _save(job_id: str, ledger: dict[str, Any]) -> dict[str, Any]:
    ledger = {**ledger, "updatedAt": now_iso()}
    job_store.update_job(job_id, {"plan": ledger})
    return ledger


def update_ledger(job_id: str, *, steps: Any = None, found: Any = None,
                  open_questions: Any = None) -> dict[str, Any] | None:
    """Replace the plan (when `steps` is given — the whole list, with each step's status), add
    to what has been found, and replace what is still open. Returns the ledger as it now
    stands, plus `dropped` when older findings had to make room."""
    job = job_store.get_job(job_id)
    if job is None:
        return None
    ledger = ledger_of(job)
    if steps is not None:
        rebuilt = []
        for entry in steps if isinstance(steps, (list, tuple)) else []:
            if isinstance(entry, str):
                entry = {"text": entry}
            if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
                continue
            status = str(entry.get("status") or "todo").lower()
            rebuilt.append({"text": _short(entry["text"], ITEM_CHARS),
                            "status": status if status in STEP_STATUSES else "todo",
                            **({"note": _short(entry["note"], ITEM_CHARS)} if entry.get("note") else {})})
        ledger["steps"] = rebuilt[:MAX_STEPS]
    dropped = 0
    if found is not None:
        merged = ledger["found"] + [line for line in _lines(found, MAX_FOUND) if line not in ledger["found"]]
        dropped = max(0, len(merged) - MAX_FOUND)
        ledger["found"] = merged[-MAX_FOUND:]
    if open_questions is not None:
        ledger["open"] = _lines(open_questions, MAX_OPEN)
    saved = _save(job_id, ledger)
    done = sum(1 for s in saved["steps"] if s["status"] == "done")
    if saved["steps"]:
        job_store.update_job(job_id, {"progress": round(100 * done / len(saved["steps"]))})
    return {**saved, **({"dropped": dropped} if dropped else {})}


def note_round(job_id: str, text: str) -> None:
    """Keep a round's own progress note — written by code at the end of every round that ran
    out of room, so the ledger has where the work stands even if the worker never updates it.
    The notes before it are kept too (a few): a job's working transcript holds only its newest
    messages, and on long work the notes are what is left of the rounds that scrolled out.
    Found by the proving run: a stand-in following a 34-record trail lost its early reads from
    the transcript and started again from the first record, for ever."""
    job = job_store.get_job(job_id)
    note = clean(text)
    if job is None or not note:
        return
    ledger = ledger_of(job)
    earlier = ledger["earlier"] + ([ledger["note"]] if ledger["note"] and ledger["note"] != note else [])
    _save(job_id, {**ledger, "note": note[:NOTE_CHARS], "earlier": earlier[-EARLIER_NOTES:]})


def _activity(job_id: str) -> str:
    """What the work has actually done so far, counted from its own trace (never from what it
    says it did): each action, how often it ran and how often it failed."""
    counts: dict[str, list[int]] = {}
    for row in job_store.get_trace(job_id):
        if row.get("kind") != "tool" or row.get("phase") != "outcome":
            continue
        detail = row.get("detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                continue
        if not isinstance(detail, dict) or not detail.get("name"):
            continue
        tally = counts.setdefault(str(detail["name"]), [0, 0])
        tally[0] += 1
        tally[1] += 0 if detail.get("ok") else 1
    parts = [f"{name} ×{ran}" + (f" ({failed} failed)" if failed else "")
             for name, (ran, failed) in counts.items()]
    return ", ".join(parts)


def set_blocked(job_id: str, reason: str, tried: Any = None) -> dict[str, Any] | None:
    """The work itself says it cannot be finished: what is in the way, and what it tried."""
    job = job_store.get_job(job_id)
    if job is None:
        return None
    blocked = {"reason": _short(reason, 600), "tried": _lines(tried, 8)}
    _save(job_id, {**ledger_of(job), "blocked": blocked})
    return blocked


def clear_blocked(job_id: str) -> None:
    job = job_store.get_job(job_id)
    if job is not None and ledger_of(job)["blocked"]:
        _save(job_id, {**ledger_of(job), "blocked": None})


def blocked_of(job: dict[str, Any] | None) -> dict[str, Any] | None:
    return ledger_of(job)["blocked"]


def progress_mark(job_id: str) -> dict[str, int]:
    """Where the work stands, as numbers that only go up when it really moves: plan steps
    done, findings kept, and distinct actions that succeeded (from the trace — the same call
    made five times is one)."""
    job = job_store.get_job(job_id)
    ledger = ledger_of(job)
    started: dict[str, str] = {}
    worked: set[str] = set()
    for row in job_store.get_trace(job_id):
        if row.get("kind") != "tool":
            continue
        detail = row.get("detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                continue
        if not isinstance(detail, dict):
            continue
        operation = str(detail.get("operationId") or "")
        if row.get("phase") == "intent":
            started[operation] = f"{detail.get('name')}:{json.dumps(detail.get('args') or {}, sort_keys=True, default=str)}"
        elif detail.get("ok") and operation in started:
            worked.add(started[operation])
    return {"done": sum(1 for s in ledger["steps"] if s["status"] == "done"),
            "found": len(ledger["found"]), "calls": len(worked)}


# --- in front of the worker ---------------------------------------------------------------------

def job_for_session(session_id: str) -> dict[str, Any] | None:
    if not str(session_id or "").startswith(SESSION_PREFIX):
        return None
    return job_store.get_job(session_id[len(SESSION_PREFIX):])


def worker_sections(session_id: str) -> list[tuple[str, str]]:
    """The objective and the ledger as prompt sections for a job's own turn; empty for any
    other session. Never raises: a turn must not fail because of this."""
    try:
        job = job_for_session(session_id)
    except Exception:  # noqa: BLE001
        return []
    if job is None:
        return []
    brief, ledger = brief_of(job), ledger_of(job)

    objective = [f"What you are working on: {brief['outcome']}"]
    if brief["doneWhen"]:
        objective.append("It is done when ALL of this is true:\n"
                         + "\n".join(f"- {line}" for line in brief["doneWhen"]))
    else:
        objective.append("Nobody said what done looks like. Decide the plainest reading of it, "
                         "write it as your first plan step, and work to that.")
    if brief["said"]:
        objective.append("What Boss actually said (their own words):\n"
                         + "\n".join(f'- "{line}"' for line in brief["said"]))
    if brief["context"]:
        objective.append(f"What was settled before you started:\n{brief['context']}")
    if brief["already"]:
        objective.append("Already done before this was handed to you — use it, do not redo it:\n"
                         + brief["already"])

    progress = []
    if ledger["steps"]:
        progress.append("Your plan:\n" + "\n".join(
            f"{n}. [{step['status']}] {step['text']}" + (f" — {step['note']}" if step.get("note") else "")
            for n, step in enumerate(ledger["steps"], start=1)))
    if ledger["found"]:
        progress.append("What you have found so far:\n" + "\n".join(f"- {line}" for line in ledger["found"]))
    if ledger["open"]:
        progress.append("Still open:\n" + "\n".join(f"- {line}" for line in ledger["open"]))
    if ledger["earlier"]:
        progress.append("Your notes from earlier rounds, oldest first:\n"
                        + "\n".join(f"- {note}" for note in ledger["earlier"]))
    if ledger["note"]:
        progress.append(f"Your note at the end of the last round:\n{ledger['note']}")
    try:
        done = _activity(job["id"])
    except Exception:  # noqa: BLE001
        done = ""
    if done:
        progress.append(f"What you have actually run so far (from the record): {done}")

    sections = [("objective", "\n\n".join(objective))]
    if progress:
        sections.append(("where_it_stands", "\n\n".join(progress)))
    return sections
