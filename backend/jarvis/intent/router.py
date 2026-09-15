"""Deciding what a request actually is, before spending a model call on it.

§10: "Do not send every request through maximum reasoning... 'What's the time?'
'Open Chrome.' 'What's my battery level?' 'Turn the volume down.' These should use
lightweight deterministic pathways where possible."

Today there is no such path: every utterance, including "what's the time", is a
full model round trip that has to decide to call a tool. That is the single
biggest reason the assistant does not feel responsive.

**The governing bias of this module is that a wrong fast path is worse than a
slow one.** A deterministic shortcut that swallows a nuanced request produces a
confidently wrong answer; falling through to the model merely costs a second. So
every rule here is written to REFUSE on any hint of complication — a conjunction,
a condition, a qualifier, a comparison, a follow-up clause — and the tests are
mostly about what it declines to match.

**What this module honestly does and does not do.** The fast path is fully
deterministic over a narrow, curated set. The broader §11 classification
(RESEARCH vs MULTI_STEP vs BACKGROUND_JOB) is NOT something a regex can decide
reliably, and pretending otherwise would be fake intelligence of exactly the kind
§45 forbids. For those, this returns a low-confidence hint and says so; the
orchestrator treats a hint as a suggestion and lets the model decide.

Pure and dependency-free by design, so the whole rule set can be exercised as a
truth table — the pattern the audit found to be the most reliable thing in this
codebase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Intent(str, Enum):
    """§11's routing categories."""

    CHAT = "chat"
    COMMAND = "command"
    TOOL_ACTION = "tool_action"
    RESEARCH = "research"
    BACKGROUND_JOB = "background_job"
    MULTI_STEP = "multi_step"
    MEMORY = "memory"
    SYSTEM_CONTROL = "system_control"
    PROACTIVE = "proactive"
    CLARIFY = "clarify"


@dataclass(frozen=True)
class FastPath:
    """A capability that can be run directly, with no model call at all."""

    capability: str
    args: dict[str, object]


@dataclass(frozen=True)
class Route:
    intent: Intent
    #: 1.0 only for a deterministic fast-path match. Everything else is a hint.
    confidence: float
    reason: str
    fast_path: FastPath | None = None

    @property
    def is_fast(self) -> bool:
        return self.fast_path is not None


# --- what disqualifies a request from EVER taking the fast path --------------
#
# Each of these means the utterance carries more meaning than the shortcut can
# honour. "What's the time in Tokyo" is not "what's the time"; "open Chrome and
# find the doc" is not "open Chrome".

_COMPLICATIONS = re.compile(
    r"""
      \b(and|then|also|after|before|while|plus|as\ well\ as)\b   # more than one thing
    | \b(if|unless|when|whenever|in\ case|depending)\b            # a condition
    | \b(why|how\ come|explain|compare|versus|vs|instead|rather)\b  # wants reasoning
    | \b(every|each|all\ of|recurring|daily|weekly|remind)\b      # a rule, not an act
    | \b(yesterday|tomorrow|later|tonight|next|last)\b            # not "now"
    | [?].*[?]                                                    # more than one question
    | \b(in|at|for|on)\s+\w+                                      # a qualifier: "in Tokyo"
    """,
    re.IGNORECASE | re.VERBOSE,
)

_MAX_FAST_WORDS = 6


def _too_complicated(text: str) -> str | None:
    """Return the reason this cannot be fast-pathed, or None."""
    if len(text.split()) > _MAX_FAST_WORDS:
        return "longer than a simple command"
    found = _COMPLICATIONS.search(text)
    if found:
        return f"carries a qualifier or extra clause ({found.group(0).strip()!r})"
    return None


# --- the curated deterministic set -------------------------------------------
#
# Deliberately small. Every entry is a phrasing whose meaning is unambiguous and
# whose capability takes no interpretation. Growing this list is a decision, not
# a convenience.

_TIME = re.compile(r"^\s*(what(?:'s| is)\s+the\s+time|what\s+time\s+is\s+it|the\s+time)\s*[?.!]*\s*$", re.I)
_DATE = re.compile(r"^\s*(what(?:'s| is)\s+(?:the|today'?s)\s+date|what\s+day\s+is\s+it)\s*[?.!]*\s*$", re.I)
_OPEN = re.compile(r"^\s*(?:please\s+)?(?:open|launch|start)\s+(?P<target>[\w.\- ]{1,30}?)\s*[?.!]*\s*$", re.I)
_VOLUME = re.compile(r"^\s*(?:please\s+)?(?:turn\s+(?:the\s+)?)?volume\s+(?P<dir>up|down)\s*[?.!]*\s*$", re.I)
_MUTE = re.compile(r"^\s*(?:please\s+)?(mute|unmute)\s*[?.!]*\s*$", re.I)
_BATTERY = re.compile(r"^\s*(what(?:'s| is)\s+(?:my\s+)?battery(?:\s+level)?|battery(?:\s+level)?)\s*[?.!]*\s*$", re.I)

#: Words that look like an app name but are really a topic — "open the discussion"
#: is not a launch request.
_NOT_APPS = frozenset({
    "it", "that", "this", "them", "up", "door",
    "discussion", "conversation", "question", "issue", "topic", "case",
})

#: Stripped before judging, never a name on their own.
_ARTICLES = frozenset({"the", "a", "an", "my", "your"})


def _app_name(target: str) -> str | None:
    """The real app name inside a captured `open X` target, or None if X is a topic.

    Checked token-wise, not as one string: the whole phrase "the discussion" is
    not in _NOT_APPS even though "discussion" is, so a whole-string membership
    test lets a leading article smuggle a topic through. Any token being a
    non-app word disqualifies the phrase — "open the settings discussion" is no
    more a launch request than "open the discussion" is.
    """
    tokens = [t for t in target.lower().split() if t]
    while tokens and tokens[0] in _ARTICLES:
        tokens.pop(0)
    if not tokens:
        return None
    if any(t in _NOT_APPS for t in tokens):
        return None
    # Return the user's own casing for the surviving tokens, not the lowered copy.
    return " ".join(target.split()[-len(tokens):])


def _fast_match(text: str) -> tuple[Intent, FastPath, str] | None:
    if _TIME.match(text):
        return Intent.TOOL_ACTION, FastPath("get_time", {}), "asked for the time"
    if _DATE.match(text):
        return Intent.TOOL_ACTION, FastPath("get_time", {"include_date": True}), "asked for the date"
    if _BATTERY.match(text):
        return Intent.SYSTEM_CONTROL, FastPath("system_info", {"field": "battery"}), "asked for battery level"

    volume = _VOLUME.match(text)
    if volume:
        direction = volume.group("dir").lower()
        return Intent.SYSTEM_CONTROL, FastPath("set_volume", {"direction": direction}), f"volume {direction}"

    mute = _MUTE.match(text)
    if mute:
        action = mute.group(1).lower()
        return Intent.SYSTEM_CONTROL, FastPath("set_volume", {"direction": action}), action

    opened = _OPEN.match(text)
    if opened:
        name = _app_name(opened.group("target").strip())
        if name is None:
            return None
        return Intent.COMMAND, FastPath("open_app", {"name": name}), f"open {name}"
    return None


# --- weak hints for everything else ------------------------------------------
#
# Explicitly hints, not decisions. Confidence stays low so the orchestrator knows
# to let the model settle it.

_HINTS: list[tuple[re.Pattern[str], Intent]] = [
    (re.compile(r"\b(remember|forget|what do you (?:know|remember))\b", re.I), Intent.MEMORY),
    (re.compile(r"\b(research|look into|find out about|investigate)\b", re.I), Intent.RESEARCH),
    (re.compile(r"\b(in the background|keep working|while you|carry on with)\b", re.I), Intent.BACKGROUND_JOB),
    (re.compile(r"\b(and then|after that|step by step|first.*then)\b", re.I), Intent.MULTI_STEP),
]


def classify(text: str) -> Route:
    """Route one utterance. Pure: no model call, no I/O, no state."""
    stripped = (text or "").strip()
    if not stripped:
        return Route(Intent.CLARIFY, 1.0, "nothing was said")

    blocked = _too_complicated(stripped)
    if blocked is None:
        matched = _fast_match(stripped)
        if matched is not None:
            intent, fast, why = matched
            return Route(intent, 1.0, why, fast_path=fast)

    for pattern, intent in _HINTS:
        if pattern.search(stripped):
            return Route(
                intent, 0.4,
                f"looks like {intent.value}, but the model should confirm",
            )

    reason = "no deterministic match"
    if blocked:
        reason = f"not a simple command: {blocked}"
    return Route(Intent.CHAT, 0.0, reason)
