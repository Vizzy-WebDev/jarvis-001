"""Was that answer consequential enough to be worth checking?

Pure, and deliberately conservative. Semantic verification costs a model call, on
a roster that is routinely rate limited, so the question is not "could this be
worth checking" but "would a person care if this specific answer were wrong".

The signals are all things the turn already knows about itself — no extra model
call to decide whether to spend a model call, which would be its own absurdity.

Two floors, both structural:

* a **daily budget**, so a busy day cannot turn this into a per-turn cost;
* the **preference**, which is off by default. The owner asked for this to exist
  and to be theirs to switch on, not for it to start spending on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..db import get_db
from ..jscompat import now_iso

#: Calls per day. Sized so that turning this on is a visible, bounded cost
#: rather than an open one.
DAILY_BUDGET = 20
_BUDGET_KEY = "ops.verification.budget"

#: A reply shorter than this is a "yes", an acknowledgement or a one-liner —
#: there is nothing in it for a check to disagree with.
MINIMUM_ANSWER_CHARS = 200

#: Tools whose use says the turn DID something, rather than looked something up.
#: A turn that only read the clock is not consequential however long the answer.
_TRIVIAL_TOOLS = frozenset({
    "get_time", "check_myself", "track_goal", "find_capability", "check_spending",
    "check_environment", "check_my_health", "acknowledge_notice",
})


@dataclass(frozen=True)
class Judgment:
    worth_checking: bool
    reason: str


def is_consequential(*, answer_text: str, tool_names: Iterable[str] = (),
                     produced_artifact: bool = False) -> Judgment:
    """Pure: no I/O, no budget, no preference. Just "does this matter"."""
    used = [name for name in tool_names if name not in _TRIVIAL_TOOLS]

    if produced_artifact:
        return Judgment(True, "it produced a real file")
    if used:
        return Judgment(True, f"it actually did something ({', '.join(sorted(set(used)))})")
    if len(answer_text or "") >= MINIMUM_ANSWER_CHARS:
        return Judgment(True, "it is a substantial answer someone may act on")
    return Judgment(False, "a short answer with nothing done and nothing produced")


# --- the budget ---------------------------------------------------------------

def _read() -> dict[str, Any]:
    row = get_db().execute("SELECT value FROM app_state WHERE key = ?", (_BUDGET_KEY,)).fetchone()
    if row is None:
        return {"day": "", "used": 0}
    import json
    try:
        return json.loads(row["value"])
    except ValueError:
        return {"day": "", "used": 0}


def _write(state: dict[str, Any]) -> None:
    import json
    get_db().execute("INSERT INTO app_state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (_BUDGET_KEY, json.dumps(state)))


def _today() -> str:
    return now_iso()[:10]


def budget_remaining() -> int:
    state = _read()
    if state.get("day") != _today():
        return DAILY_BUDGET
    return max(0, DAILY_BUDGET - int(state.get("used") or 0))


def try_consume_budget() -> bool:
    state = _read()
    if state.get("day") != _today():
        state = {"day": _today(), "used": 0}
    if int(state.get("used") or 0) >= DAILY_BUDGET:
        return False
    state["used"] = int(state.get("used") or 0) + 1
    _write(state)
    return True


def is_enabled() -> bool:
    """The preference. Default off — see this module's docstring."""
    from ..prefs import get_prefs

    return bool(get_prefs().get("verifyChatAnswers"))
