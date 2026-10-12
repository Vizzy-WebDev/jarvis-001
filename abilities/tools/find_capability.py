"""Find one of the less common abilities by what the user actually wants.

Declaring every capability on every turn is not free: measured, the full
set was ~150,000 characters sent on "hello" as much as on anything else,
and it was the single largest cause of flat, instruction-ignoring replies. So a
large registry declares its core set plus whatever this turn has unlocked, and
this tool is how the rest becomes reachable.

**How unlocking works here, and why it is not a `ctx` injection.** This tool
returns the names it matched under an `unlock` key; the orchestrator adds them to
the tools declared for the remaining steps of that turn. The mechanism is
therefore plain data in a tool result — testable on its own, with no tool
reaching back into the loop that called it, and no import edge back to the
registry loader.
"""

from __future__ import annotations

import re
from typing import Any

from ..capabilities import CapabilityRegistry, CapabilitySpec, Risk

MAX_MATCHES = 6
_WORD = re.compile(r"[a-z0-9]+")

#: Filler that appears in nearly every capability description, so matching on it
#: returns the whole registry — which is the same as returning nothing useful.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "your", "you", "this", "that", "what",
    "when", "how", "can", "use", "user", "asks", "about", "them", "something",
    "anything", "please", "want", "wants", "need", "needs", "help", "me", "my",
})


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower())
            if len(w) > 2 and w not in _STOPWORDS]


def _score(spec: CapabilitySpec, terms: list[str]) -> int:
    name = spec.name.lower().replace("_", " ")
    description = (spec.description or "").lower()
    score = 0
    for term in terms:
        if term in name:
            score += 3
        if term in description:
            score += 1
    return score


BRIEF_CHARS = 240


def _label_words(label: str) -> list[str]:
    return [w for w in _WORD.findall((label or "").lower()) if len(w) > 2]


def _named_but_unusable(terms: list[str]) -> list[dict[str, str]]:
    """Apps the person NAMED that Jarvis cannot use right now, and why.

    Matching is on the app's own label (every word of it must be in what was asked),
    over what the person saved in Connector and the Official directory. Nothing here
    knows a service: it reads the same records the Connector screen shows. Without
    it, naming an app that was not set up returned whatever else happened to mention
    the word, which is how an aggregator's "500+ apps incl. GitHub" got used instead.
    """
    try:
        from ..connectors import catalog, store
        from ..connectors.capabilities import tool_rows
    except Exception:  # noqa: BLE001 — a broken connector store must not break the search
        return []
    wanted = set(terms)
    seen: set[str] = set()
    out: list[dict[str, str]] = []

    def named(label: str) -> bool:
        words = _label_words(label)
        return bool(words) and all(word in wanted for word in words)

    try:
        saved = [c for c in store.list_connectors() if c.get("type") in store.USER_TYPES]
    except Exception:  # noqa: BLE001
        saved = []
    for connector in saved:
        label = str(connector.get("label") or "")
        if not named(label):
            continue
        status = connector.get("status") or {}
        if not connector.get("enabled", True):
            state = "switched off in Connector"
        elif status.get("state") == "error":
            reason = str(status.get("detail") or "").strip()
            state = "needs reconnecting" + (f": {reason[:160]}" if reason else "")
        elif status.get("state") != "working" and connector.get("type") == "mcp":
            state = "added but not connected yet"
        else:
            try:
                usable = bool(tool_rows(connector))
            except Exception:  # noqa: BLE001
                usable = True
            if usable:
                seen.add(label.lower())
                continue
            state = "connected, but its tools have not been read yet"
        seen.add(label.lower())
        out.append({"app": label, "state": state})
    for entry in catalog.list_catalog():
        label = str(entry.get("label") or "")
        if label.lower() in seen or not named(label):
            continue
        has_record = any((c.get("source") or {}).get("id") == entry.get("id") for c in saved)
        if not has_record:
            out.append({"app": label, "state": "not set up yet"})
    return out


def _unusable_note(apps: list[dict[str, str]]) -> str:
    names = ", ".join(f'{a["app"]} ({a["state"]})' for a in apps)
    return (f"The user named an app Jarvis cannot use right now: {names}. Say so plainly and offer "
            "to help set it up in Connector. Do not quietly do the job through a different app "
            "(an aggregator that lists many apps, say) unless the user says that is fine.")


def _brief(description: str) -> str:
    """Enough to choose by, not the whole manual.

    A connected app's own tool description can run to thousands of characters
    (Notion's longest is ~8,700): returned in full, one search for "create a
    Notion page" was ~19,000 characters, and every tool it unlocks is declared
    right after WITH that full description anyway — the model paid for it twice,
    on the free tiers this app usually runs on.
    """
    lines = [line.strip() for line in (description or "").splitlines()]
    text = " ".join(line for line in lines if line and not line.startswith("#"))
    return text if len(text) <= BRIEF_CHARS else text[:BRIEF_CHARS].rsplit(" ", 1)[0] + "…"


def build(registry: CapabilityRegistry) -> list[CapabilitySpec]:
    def _run(intent: str = "") -> dict[str, Any]:
        terms = _terms(intent)
        if not terms:
            # Same shape as "nothing matched", deliberately: one result shape
            # means no caller has to special-case an empty search.
            return {"ok": False, "found": [], "unlock": [],
                    "error": "Say in a few words what's needed — a verb helps."}
        # A background worker's own tools are not answers to "can you do X":
        # nothing the user says can reach one, so offering it would be a lie.
        searchable = [spec for spec in registry.list() if not spec.has_tag("job")]
        scored = [(spec, _score(spec, terms)) for spec in searchable]
        best = max((n for _, n in scored), default=0)
        # Two gates, both needed: a hard floor so one incidental word in a long
        # description is not a match, and a relative floor so a genuinely strong
        # match is not diluted by six weak ones.
        floor = max(2, best / 2)
        matches = sorted(((s, n) for s, n in scored if n >= floor),
                         key=lambda pair: (-pair[1], pair[0].name))[:MAX_MATCHES]
        unusable = _named_but_unusable(terms)
        if not matches:
            # An honest "no" beats offering something unrelated (§45).
            answer = {"ok": True, "found": [], "unlock": [],
                      "note": f"I have nothing that does that ({intent})."}
            if unusable:
                answer.update(notConnected=unusable, note=_unusable_note(unusable))
            return answer
        answer = {
            "ok": True,
            "found": [{"name": s.name, "description": _brief(s.description)} for s, _ in matches],
            "unlock": [s.name for s, _ in matches],
            "note": "These are callable now, in this same turn.",
        }
        if unusable:
            answer.update(notConnected=unusable,
                          note=answer["note"] + " " + _unusable_note(unusable))
        return answer

    return [CapabilitySpec(
        id="builtin.find_capability",
        name="find_capability",
        description=("Look up one of your less common abilities by what the user wants — "
                     "scheduling something, controlling the computer, reading a spreadsheet, "
                     "or anything connected through another app. Describe what's needed in a "
                     "few plain words; matching abilities become callable right away, in this "
                     "same turn."),
        input_schema={"type": "object", "properties": {
            "intent": {"type": "string",
                       "description": 'A few words, e.g. "schedule a reminder".'}},
            "required": ["intent"]},
        risk=Risk.LOW,
        handler=_run,
        timeout_s=5.0,
        tags=frozenset({"core"}),
    )]
