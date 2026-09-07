"""Checking a consequential answer after it has been given.

**After, not before.** Gating a reply on a model call would put a second round
trip in front of every substantial answer — and it would mean the turn loop
importing the verification subsystem, which is exactly the coupling the event bus
exists to avoid and which the fitness tests forbid. So the check runs on the
event the loop already publishes, and its result is recorded rather than used to
withhold anything.

That trade-off is real and worth stating plainly: this catches a wrong answer
after the person has read it. What it buys is a record — the mismatch is traced
and surfaced — instead of nothing at all, which is what the alternative
("verify chat answers, but only in a way that never delays a reply") reduces to.

Off by default: `prefs.verifyChatAnswers`.
"""

from __future__ import annotations

import logging
import threading

from ..events.bus import Event

logger = logging.getLogger(__name__)

#: Verification runs off the turn's own thread. A model call on the publishing
#: thread would stall whatever came after the reply.
_threads: list[threading.Thread] = []


def verify_answer(event: Event) -> None:
    from ..ops import consequence

    if not consequence.is_enabled():
        return

    answer = str(event.payload.get("text") or "")
    request = str(event.payload.get("userText") or "")
    if not answer or not request:
        return

    judgment = consequence.is_consequential(
        answer_text=answer, tool_names=event.payload.get("toolNames") or ())
    if not judgment.worth_checking:
        return
    if not consequence.try_consume_budget():
        logger.info("skipping a semantic check: the daily budget is spent")
        return

    thread = threading.Thread(target=_run, args=(request, answer, event), daemon=True,
                              name="verify-answer")
    _threads.append(thread)
    thread.start()


def _run(request: str, answer: str, event: Event) -> None:
    from ..ops import trace as ops_trace
    from ..ops.verify import verify_semantic_match

    try:
        verdict = verify_semantic_match(request=request, result_summary="a reply in conversation",
                                        result_text=answer)
    except Exception:  # noqa: BLE001 — an observer must never take down anything
        logger.exception("the semantic check itself failed")
        return

    if not verdict.checked:
        return                           # could not check: never recorded as either

    turn_id = str(event.payload.get("turnId") or "")
    ops_trace.append(
        source="verification", source_ref=turn_id or None, phase="outcome", effect="read",
        kind="decision",
        summary=("The answer matched what was asked." if verdict.matches
                 else "The answer did NOT match what was asked."),
        detail={"reason": verdict.reason, "sessionId": event.payload.get("sessionId")})

    if verdict.matches:
        return

    from ..heartbeat import outbox

    outbox.add(source="verification", source_ref=turn_id or None, tier=2, reason="notice",
               summary=("Something I said may not have actually answered what you asked: "
                        + (verdict.reason or "it does not match the request.")),
               detail={"turnId": turn_id})


def join_all(timeout: float = 10.0) -> bool:
    """Wait for outstanding checks — for shutdown and between tests."""
    alive = [t for t in _threads if t.is_alive()]
    for thread in alive:
        thread.join(timeout)
    _threads.clear()
    return all(not t.is_alive() for t in alive)
