"""Checking a finished piece of work against what was asked — from what it DID, not what it says.

"It finished" and "it did what was asked" are different claims, and the worker's own report is
evidence for neither. So a result is checked in three parts, each weaker than the one before:

1. **The record** (`evidence`). Built by code from the job's write-ahead trace: every action
   it really ran, with its arguments, whether it worked, and whether it could reach outside
   Jarvis. Nothing in it comes from the worker's words.
2. **What needs no model** (`findings`). Facts read straight off the record: an outward action
   that failed and was never put right; the same outward action done more than once.
3. **One judgement, line by line** (`judge`). A model is given what was asked, each line that
   defines done, the worker's report AND the record, told to trust the record over the report,
   and answers every line separately: met, not met, or cannot tell.

The verdict (`check`) is then one of four, and is always said when the result is delivered
(`label`) — nothing is called checked that was not:

- `verified` — every line met, and the record shows nothing left failed.
- `partly`   — some lines met; the rest are named as not done or not confirmed.
- `unmet`    — nothing asked for was shown to be done.
- `unchecked` — no model could check it; the result is the worker's own account.

A result with lines not met is sent back ONCE PER ALLOWED RECOVERY with only those lines
(`send_back`), and after that it is delivered as it stands, labelled — never held as "stuck"
over a check, which is itself a judgement that can be wrong.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..jscompat import now_iso
from . import job_store, objective

logger = logging.getLogger(__name__)

VERDICTS = ("verified", "partly", "unmet", "unchecked")
LINE_VERDICTS = ("met", "not_met", "unknown")

#: How much of each thing the judge is shown. The record is listed action by action for what
#: could reach outside Jarvis (those are what "done" usually turns on) and counted for reads.
ARGS_CHARS = 400
EVIDENCE_ACTIONS = 40
REPORT_CHARS = 6000

SYSTEM = (
    "You check a finished piece of work against what was asked. You are given: what was asked; "
    "the numbered lines that define done; the worker's own report; and a RECORD, made by the "
    "system and not by the worker, of every action the work actually ran and whether it "
    "succeeded. Trust the record over the report: a claim the record contradicts, or an action "
    "the report says happened that the record does not show succeeding, is not met. Judge each "
    "line on its own, and only whether it was achieved — not style, not how the work was "
    "divided up, not whether it ran in the background, not how or when the person is told. "
    "Use \"unknown\" only when neither the record nor the report can settle the line. Reply "
    'with JSON only: {"lines": [{"n": 1, "verdict": "met" | "not_met" | "unknown", '
    '"why": "one short sentence"}]} — one entry for every numbered line.')


# --- the record -------------------------------------------------------------------------------

def _rows(job_id: str) -> list[dict[str, Any]]:
    out = []
    for row in job_store.get_trace(job_id):
        if row.get("kind") != "tool":
            continue
        detail = row.get("detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                continue
        if isinstance(detail, dict) and detail.get("name"):
            out.append({"phase": row.get("phase"), "effect": row.get("effect"), **detail})
    return out


def evidence(job_id: str) -> dict[str, Any]:
    """What the work actually did, from its trace: each action in order with its arguments and
    how it ended. `outward` are the ones that could change something (MEDIUM/HIGH risk)."""
    started: dict[str, dict[str, Any]] = {}
    actions: list[dict[str, Any]] = []
    for row in _rows(job_id):
        operation = str(row.get("operationId") or "")
        if row["phase"] == "intent":
            started[operation] = {"name": str(row["name"]), "args": row.get("args") or {},
                                  "outward": row.get("effect") == "external"}
        elif operation in started:
            begun = started[operation]
            how = "ok" if row.get("ok") else ("not_run" if row.get("redo") or str(
                row.get("error") or "").startswith("refused") else "failed")
            actions.append({**begun, "result": how, "error": row.get("error")})
    done_ops = {str(r.get("operationId") or "") for r in _rows(job_id) if r["phase"] == "outcome"}
    # Started and never recorded as ended: it may or may not have happened.
    unsure = [a for op, a in started.items() if op not in done_ops and a["outward"]]
    return {"actions": actions, "unsure": unsure}


def _sig(action: dict[str, Any]) -> str:
    return objective._signature(action["name"], action["args"])


def _shown(action: dict[str, Any]) -> str:
    args = json.dumps(action["args"], sort_keys=True, default=str, ensure_ascii=False)
    return f"{action['name']}({args[:ARGS_CHARS]})"


def findings(record: dict[str, Any]) -> list[dict[str, str]]:
    """Facts that need no model, read off the record: `{"kind", "text"}` with kind `failed`
    (an outward action that failed and was never put right), `unsure` (started, no recorded
    result) or `twice` (the same outward action done more than once — which may be exactly
    what was asked, so it is reported and takes nothing away)."""
    out: list[dict[str, str]] = []
    outward = [a for a in record["actions"] if a["outward"]]
    worked = {_sig(a) for a in outward if a["result"] == "ok"}
    failed = {}
    for action in outward:
        if action["result"] == "failed" and _sig(action) not in worked:
            failed[_sig(action)] = action
    for action in failed.values():
        out.append({"kind": "failed", "text": f"{_shown(action)} failed and was never done successfully"
                    + (f" ({action['error']})" if action.get("error") else "")})
    counts: dict[str, int] = {}
    for action in outward:
        if action["result"] == "ok":
            counts[_sig(action)] = counts.get(_sig(action), 0) + 1
    for action in outward:
        if counts.get(_sig(action), 0) > 1:
            out.append({"kind": "twice", "text": f"{_shown(action)} was done {counts.pop(_sig(action))} times"})
    for action in record["unsure"]:
        out.append({"kind": "unsure", "text": f"{_shown(action)} was started and has no recorded "
                    "result: it may or may not have happened"})
    return out


def record_text(record: dict[str, Any]) -> str:
    """The record as the judge reads it: outward actions one by one, reads as counts."""
    outward = [a for a in record["actions"] if a["outward"]]
    reads: dict[str, list[int]] = {}
    for action in record["actions"]:
        if not action["outward"]:
            tally = reads.setdefault(action["name"], [0, 0])
            tally[0] += 1
            tally[1] += 0 if action["result"] == "ok" else 1
    lines = []
    words = {"ok": "SUCCEEDED", "failed": "FAILED", "not_run": "NOT RUN (refused)"}
    for action in outward[-EVIDENCE_ACTIONS:]:
        lines.append(f"- {_shown(action)} → {words[action['result']]}"
                     + (f": {action['error']}" if action.get("error") and action["result"] != "ok" else ""))
    if not outward:
        lines.append("- (no action that changes anything was run)")
    if reads:
        lines.append("Lookups and reads (they change nothing): " + ", ".join(
            f"{name} ×{ran}" + (f" ({bad} failed)" if bad else "") for name, (ran, bad) in reads.items()))
    return "\n".join(lines)


# --- the judgement ----------------------------------------------------------------------------

def criteria(job: dict[str, Any]) -> list[str]:
    """The lines that define done; with none given, the outcome itself is the one line."""
    brief = objective.brief_of(job)
    return list(brief["doneWhen"]) or [brief["outcome"] or str(job.get("goal") or job.get("title") or "")]


def judge(job: dict[str, Any], answer: str, record: dict[str, Any],
          facts: list[dict[str, str]]) -> list[dict[str, str]] | None:
    """Each line of done, judged against the record and the report. None when no model could
    do it or its answer could not be read — which is `unchecked`, never a pass."""
    from ..ai import ask

    brief, lines = objective.brief_of(job), criteria(job)
    prompt = "\n\n".join(filter(None, [
        f"What was asked: {brief['outcome']}",
        "Done means ALL of these:\n" + "\n".join(f"{n}. {line}" for n, line in enumerate(lines, 1)),
        ("The person's own words:\n" + "\n".join(f'- "{said}"' for said in brief["said"]))
        if brief["said"] else None,
        "THE RECORD of what the work actually ran:\n" + record_text(record),
        ("Read off the record by the system:\n" + "\n".join(f"- {fact['text']}" for fact in facts)) if facts else None,
        f"THE WORKER'S REPORT:\n{answer[:REPORT_CHARS]}",
    ]))
    try:
        data = ask(prompt, system=SYSTEM, want_json=True, data_class="personal", task_class="judge").data
    except Exception as err:  # noqa: BLE001 — no model able to check is an ordinary case
        logger.info("the check on job %s could not run: %s", job.get("id"), err)
        return None
    rows = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    by_number: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            by_number[int(row.get("n"))] = row
        except (AttributeError, TypeError, ValueError):
            continue
    if not by_number:
        return None
    out = []
    for n, line in enumerate(lines, 1):
        row = by_number.get(n) or {}
        verdict = row.get("verdict") if row.get("verdict") in LINE_VERDICTS else "unknown"
        why = row.get("why") if isinstance(row.get("why"), str) else ""
        out.append({"line": line, "verdict": verdict, "why": why.strip()[:300]})
    return out


def check(job: dict[str, Any], answer: str) -> dict[str, Any]:
    """The whole check of one finished result. Never raises."""
    try:
        record = evidence(job["id"])
        facts = findings(record)
    except Exception:  # noqa: BLE001 — a record that cannot be read checks nothing
        logger.exception("could not read what job %s did", job.get("id"))
        return {"verdict": "unchecked", "lines": [], "facts": [], "at": now_iso()}
    lines = judge(job, answer, record, facts)
    if lines is None:
        verdict = "unchecked"
        lines = []
    else:
        met = sum(1 for line in lines if line["verdict"] == "met")
        # "Left failed" and "may or may not have happened" keep a result from being called
        # verified whatever the judgement says; "done twice" is reported but takes nothing
        # away from what was achieved.
        loose = [fact for fact in facts if fact["kind"] != "twice"]
        if met == len(lines) and not loose:
            verdict = "verified"
        elif met == 0 and any(line["verdict"] == "not_met" for line in lines):
            verdict = "unmet"
        else:
            verdict = "partly"
    return {"verdict": verdict, "lines": lines, "facts": facts, "at": now_iso()}


def unmet(result: dict[str, Any]) -> list[dict[str, str]]:
    return [line for line in result.get("lines") or [] if line["verdict"] == "not_met"]


def send_back(result: dict[str, Any]) -> str:
    """What the worker is told when its result goes back: only the lines not met, and that
    everything else stands."""
    parts = [f'"{line["line"]}"' + (f" — {line['why']}" if line["why"] else "") for line in unmet(result)]
    kept = [line["line"] for line in result.get("lines") or [] if line["verdict"] == "met"]
    text = "Not yet done: " + "; ".join(parts) + "."
    if kept:
        text += " Already done and not to be touched again: " + "; ".join(f'"{k}"' for k in kept) + "."
    return text


def aloud(result: dict[str, Any] | None) -> str:
    """What is SAID with a result Jarvis announces: nothing when it all checked out, and
    otherwise the one thing the person must not be left to assume."""
    verdict = (result or {}).get("verdict")
    if verdict in (None, "verified"):
        return ""
    if verdict == "unchecked":
        return "I could not check it, so this is its own account."
    return label(result)


def label(result: dict[str, Any] | None) -> str:
    """The one line said with a delivered result: what the check found, in plain words."""
    result = result or {}
    verdict, lines = result.get("verdict"), result.get("lines") or []
    if verdict == "verified":
        return "Checked against what you asked: all of it is done."
    if verdict in ("partly", "unmet"):
        met = [line for line in lines if line["verdict"] == "met"]
        missing = [line for line in lines if line["verdict"] == "not_met"]
        unsure = [line for line in lines if line["verdict"] == "unknown"]
        text = f"Checked against what you asked: {len(met)} of {len(lines)} done."
        if missing:
            text += " Not done: " + "; ".join(line["line"] for line in missing) + "."
        if unsure:
            text += " Could not be confirmed: " + "; ".join(line["line"] for line in unsure) + "."
        loose = [fact["text"] for fact in result.get("facts") or [] if fact["kind"] != "twice"]
        if loose:
            text += " Also: " + "; ".join(loose) + "."
        return text
    return ("Not checked: nothing was able to check this result, so it is the work's own "
            "account of what it did.")
