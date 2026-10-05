"""Two questions asked before every action: how risky, and is it off-limits.

Three tiers, and the middle one is the point of having three: **safe** (look,
focus, scroll — nothing to confirm), **notable** (click, type, launch — visible
and reversible, so it is shown as it happens rather than gated), and **risky**
(delete, send, pay, install, or anything that cannot be taken back — a real
confirmation before it runs).

**The word lists live in `connectors/risk.py` and are imported, not copied.**
Both places answer the same question about text nobody here wrote, and both got
the same two bugs when they were separate: substring matching that read "share"
inside "SharePoint", and a camelCase split applied to prose that re-created that
exact false positive after it had been fixed once. One vocabulary, one set of
rules about identifiers versus prose, one place to fix a third time.

What this file adds is what is specific to operating a desktop:

* **A `type` action's label is the literal text being typed** — ordinary
  sentences are full of "write", "buy", "update" and "post", and scanning them
  with the everyday-language list made nearly everything typed require a
  confirmation. Typed text is checked only against command fragments, which is
  what the scan was for in the first place: catching `rm -rf` going into a
  terminal, not catching a shopping list.
* **Intent is scanned even for a low-level kind.** The keyword check runs BEFORE
  the kind lookup, so "press Enter" whose stated reason is "delete this
  permanently" is risky, rather than being waved through as a keypress.
* **An unknown kind is notable, never safe.** The free pass belongs to the
  primitives that are known to be harmless.

Pure: everything it needs is passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..connectors.risk import DESTRUCTIVE_COMMANDS, IRREVERSIBLE, words

SAFE = "safe"
NOTABLE = "notable"
RISKY = "risky"
BLOCKED = "blocked"

#: The primitives, and what each is worth on its own — before intent is read.
RISK_BY_KIND: dict[str, str] = {
    "windows": SAFE, "focus": SAFE, "read_window": SAFE, "screenshot": SAFE,
    "cursor": SAFE, "idle": SAFE, "processes": SAFE, "scroll": SAFE,
    "wait": SAFE, "switch_window": SAFE, "minimize_window": SAFE,
    "restore_window": SAFE, "arrange_window": SAFE,
    "click": NOTABLE, "double_click": NOTABLE, "right_click": NOTABLE,
    "type": NOTABLE, "key": NOTABLE, "launch_app": NOTABLE,
    # Closing is not risky in itself — the session confirms separately before
    # closing anything it did not open, which is the distinction that matters.
    "close_window": NOTABLE,
}


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    risk: str
    needs_confirm: bool = False
    reason: str = ""


def classify_action_risk(action: dict[str, Any]) -> str:
    """`safe` | `notable` | `risky` for one action."""
    kind_raw = str((action or {}).get("kind") or "")
    kind = kind_raw.lower()
    label = str((action or {}).get("label") or "")
    description = str((action or {}).get("description") or "")

    if kind == "type":
        typed = label.lower()
        if any(fragment in typed for fragment in DESTRUCTIVE_COMMANDS):
            return RISKY
        # The reasoning is still prose about intent, so it is still read; the
        # typed text itself is not.
        prose = description
    else:
        prose = f"{label} {description}"

    if any(fragment in f"{kind} {prose}".lower() for fragment in DESTRUCTIVE_COMMANDS):
        return RISKY

    # An identifier keeps its casing so the camelCase split can find a boundary;
    # prose is lowered first so the same split cannot invent one inside a proper
    # noun. Two shapes, two rules — see connectors/risk.py.
    tokens = set(words(kind_raw)) | set(words(prose.lower(), split_camel_case=False))
    if tokens & IRREVERSIBLE:
        return RISKY
    return RISK_BY_KIND.get(kind, NOTABLE)


def _match(value: str | None, patterns: list[str] | tuple[str, ...]) -> str | None:
    if not value:
        return None
    lowered = str(value).lower()
    for pattern in patterns or ():
        if pattern and str(pattern).lower() in lowered:
            return str(pattern)
    return None


def check_blocklist(subject: dict[str, Any], config: dict[str, Any]) -> Verdict | None:
    """The window, process or address against the user's blocked patterns.

    `None` when nothing matched — a caller reads that as "carry on", and there is
    no truthy "not blocked" object to mistake for a block.
    """
    subject = subject or {}
    hit = _match(subject.get("windowTitle"), config.get("blockedWindowPatterns") or [])
    if hit:
        return Verdict(allowed=False, risk=BLOCKED,
                       reason=f'the window title matches a blocked pattern ("{hit}")')
    hit = _match(subject.get("processName"), config.get("blockedProcesses") or [])
    if hit:
        return Verdict(allowed=False, risk=BLOCKED,
                       reason=f'"{subject.get("processName")}" is on the blocked apps list')
    hit = _match(subject.get("url"), config.get("blockedUrlPatterns") or [])
    if hit:
        return Verdict(allowed=False, risk=BLOCKED,
                       reason=f'the address matches a blocked pattern ("{hit}")')
    return None


def evaluate(action: dict[str, Any], subject: dict[str, Any],
             config: dict[str, Any]) -> Verdict:
    """One action against one on-screen subject.

    The blocklist wins outright: a blocked window is refused however harmless
    the action itself would otherwise be.
    """
    blocked = check_blocklist(subject, config)
    if blocked is not None:
        return Verdict(allowed=False, risk=BLOCKED,
                       reason=f"I won't act there — {blocked.reason}.")
    risk = classify_action_risk(action)
    return Verdict(allowed=True, risk=risk, needs_confirm=risk == RISKY)
