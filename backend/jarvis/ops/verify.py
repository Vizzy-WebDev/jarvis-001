"""Did the thing that was produced actually hold up?

Two tiers, and the distinction between them is the whole design:

* **Mechanical** — always, zero model calls. A file really opens, a script
  really exited cleanly. Cheap enough to run every time, and it catches the
  failures that matter most: a document that looks produced and cannot be
  opened is worse than an honest failure, because the person only finds out
  later.
* **Semantic** — one budgeted model call: does this genuinely answer what was
  asked? Real, but expensive on a roster that is routinely rate limited, so a
  caller decides when it is worth spending.

**"Could not check" is never reported as a pass or a fail.** No model available,
or a reply that cannot be read, returns `checked=False` — the same direction
every judgment call in this build takes when it has nothing to judge from.

No new recovery mechanism: a caller that finds a mismatch uses whatever
retry-then-escalate it already has, rather than this file inventing a second one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: How much of a result is worth showing the judge. Past this, a longer excerpt
#: costs tokens without changing the answer.
EXCERPT_CHARS = 4000


@dataclass(frozen=True)
class Verdict:
    checked: bool
    matches: bool | None
    reason: str | None = None

    @property
    def failed(self) -> bool:
        """A real, checked mismatch — never merely "could not check"."""
        return self.checked and self.matches is False


# --- mechanical ---------------------------------------------------------------

def verify_file_opens(path: Path | str) -> dict[str, Any]:
    """Re-read a generated file through this build's own reader.

    Delegates to the artifact store's own check rather than repeating it: one
    definition of "does this open", so a file kept as verified and a file
    reported as verified can never be answered by two different rules.
    """
    from ..artifacts import store

    target = Path(path)
    if not target.exists():
        return {"ok": False, "detail": "The file is not there."}
    if target.stat().st_size == 0:
        return {"ok": False, "detail": "The file is empty."}
    verified, why = store.verify(target)
    if verified is False:
        return {"ok": False, "detail": why}
    if verified is None:
        # Not checkable is not the same as verified, and is reported as such.
        return {"ok": True, "checked": False, "detail": why}
    return {"ok": True, "checked": True}


def verify_code_ran(result: Any) -> dict[str, Any]:
    """A sandboxed run's own real exit signal, named consistently with the rest
    of this file."""
    if result is None:
        return {"ok": False, "detail": "There is no result to check."}
    get = result.get if isinstance(result, dict) else lambda k, d=None: getattr(result, k, d)
    if get("timed_out") or get("timedOut"):
        return {"ok": False, "detail": "It ran out of time before finishing."}
    code = get("exit_code", get("exitCode"))
    if code not in (0, None):
        stderr = str(get("stderr") or "")[:300]
        return {"ok": False, "detail": f"It exited with code {code}" + (f": {stderr}" if stderr else "")}
    return {"ok": True}


# --- semantic -----------------------------------------------------------------

SYSTEM = ("You check whether a result actually answers what was asked. Judge only that: "
          "not style, not whether you would have done it differently. Reply with JSON only: "
          '{"matches": true|false, "reason": "one sentence"}.')


def verify_semantic_match(*, request: str, result_summary: str,
                          result_text: str | None = None) -> Verdict:
    from ..gateway.client import ask

    prompt = "\n\n".join(filter(None, [
        f'The request was: "{request}"',
        f"What was produced: {result_summary}",
        f"Its content:\n{str(result_text)[:EXCERPT_CHARS]}" if result_text else None,
    ]))

    try:
        answer = ask(prompt, system=SYSTEM, want_json=True,
                     task=_background_task(request))
    except Exception as err:  # noqa: BLE001 — no model available is the common case
        logger.info("semantic check could not run: %s", err)
        return Verdict(checked=False, matches=None)

    data = answer.data
    if not isinstance(data, dict) or not isinstance(data.get("matches"), bool):
        return Verdict(checked=False, matches=None)
    reason = data.get("reason")
    return Verdict(checked=True, matches=data["matches"],
                   reason=reason if isinstance(reason, str) and reason else None)


def _background_task(text: str) -> Any:
    from ..gateway.routing import Task

    return Task(text=text, background=True, needs_tools=False)
