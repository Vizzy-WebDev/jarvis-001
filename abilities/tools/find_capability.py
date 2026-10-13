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

import math
import re
from typing import Any

from ..capabilities import CapabilityKind, CapabilityRegistry, CapabilitySpec, Risk

MAX_MATCHES = 6
#: At most this many of one app's tools in an answer while other apps can also do the job.
PER_APP = 4
_WORD = re.compile(r"[a-z0-9]+")

#: Filler that appears in nearly every capability description, so matching on it
#: returns the whole registry — which is the same as returning nothing useful.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "your", "you", "this", "that", "what",
    "when", "how", "can", "use", "user", "asks", "about", "them", "something",
    "anything", "please", "want", "wants", "need", "needs", "help", "me", "my",
    # how a person words an instruction about WHICH app, not what they want done
    "but", "not", "dont", "don", "never", "without", "instead", "rather", "than", "other",
    "apart", "except", "using", "via", "through", "also", "just", "then", "any",
})


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower())
            if len(w) > 2 and w not in _STOPWORDS]


def _weights(specs: list[CapabilitySpec], terms: list[str]) -> dict[str, float]:
    """How much each word is worth, by how few capabilities mention it.

    Every word used to count the same, so a verb in thirty tool names ("create") outweighed
    the one word that said what was wanted ("github", found in a single description). Rarer is
    worth more, and steeply: squared, so a word in one or two capabilities beats a verb in
    dozens even when the verb is in the NAME and the rare word only in a description.
    """
    total = max(len(specs), 1)
    out: dict[str, float] = {}
    for term in set(terms):
        seen = sum(1 for s in specs
                   if term in s.name.lower().replace("_", " ") or term in (s.description or "").lower())
        out[term] = math.log((total + 1) / (seen + 1)) ** 2 + 1.0
    return out


def _score(spec: CapabilitySpec, terms: list[str], weights: dict[str, float] | None = None) -> float:
    name = spec.name.lower().replace("_", " ")
    description = (spec.description or "").lower()
    score = 0.0
    for term in terms:
        weight = (weights or {}).get(term, 1.0)
        if term in name:
            score += 3 * weight
        if term in description:
            score += 1 * weight
    return score


# --- apps the person named: to use, or to leave out ---------------------------------------------

#: Words that introduce something to leave out ("not", "don't use", "instead of", ...).
_NEGATION = re.compile(
    r"\b(?:do not|don'?t|dont|never|without|instead of|rather than|other than|apart from|except|"
    r"anything but|not)\b")


def _spec_text(spec: CapabilitySpec) -> str:
    """Everything a capability says about itself, for finding a provider or model name in it:
    its name, description and the descriptions and values in its argument schema."""
    import json

    raw = f"{spec.name} {spec.description or ''} {json.dumps(spec.input_schema or {}, default=str)}"
    return " ".join(_WORD.findall(raw.lower()))


def _persons_words(ctx: Any) -> str:
    """What the person last said in this conversation, or "" when there is none to read
    (a background run, a test, no conversation behind the call)."""
    session = getattr(ctx, "session_id", None)
    if not session:
        return ""
    try:
        from .. import conversation

        for message in reversed(conversation.get_messages(session)):
            if message.get("role") == "user":
                return str(message.get("text") or "")
    except Exception:  # noqa: BLE001 — never let reading the conversation break the search
        pass
    return ""


def _app_of(spec: CapabilitySpec) -> str | None:
    """The app a connector tool belongs to: the prefix of its `app__tool` name."""
    if spec.kind is CapabilityKind.CONNECTOR and "__" in spec.name:
        return spec.name.split("__", 1)[0].lower()
    return None


def _apps_in(specs: list[CapabilitySpec]) -> set[str]:
    return {a for a in (_app_of(s) for s in specs) if a}


def _slug(text: str) -> str:
    return "_".join(_WORD.findall((text or "").lower()))


def _resolve(name: str, apps: set[str]) -> str | None:
    """The app prefix a person's word for it points at (`git hub`, `Google Drive`)."""
    slug = _slug(name)
    if slug in apps:
        return slug
    squashed = slug.replace("_", "")
    return next((a for a in apps if a.replace("_", "") == squashed), None)


def _split_instruction(intent: str, apps: set[str]) -> tuple[str, set[str], set[str]]:
    """(the intent as words, apps to avoid, apps asked for) read from plain words.

    A negation covers an app named within a few words after it. Only a word that really is a
    connected app counts, so "not urgent" is left alone.
    """
    lowered = (intent or "").lower()
    avoid: set[str] = set()
    asked: set[str] = set()
    negated_at = [m.start() for m in _NEGATION.finditer(lowered)]
    for match in _WORD.finditer(lowered):
        found = _resolve(match.group(0), apps)
        if not found:
            continue
        before = [n for n in negated_at
                  if n < match.start() and len(_WORD.findall(lowered[n:match.start()])) <= 6]
        (avoid if before else asked).add(found)
    return " ".join(_WORD.findall(lowered)), avoid, asked - avoid


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
    def _run(intent: str = "", avoid: list[str] | None = None,
             prefer: str | None = None, ctx: Any = None) -> dict[str, Any]:
        # A background worker's own tools are not answers to "can you do X":
        # nothing the user says can reach one, so offering it would be a lie.
        searchable = [spec for spec in registry.list() if not spec.has_tag("job")]
        apps = _apps_in(searchable)
        text, left_out, asked = _split_instruction(intent, apps)
        # The person's OWN words count too. Seen live: told "use X, not Y or Z", the model searched
        # for just "generate a video with X" and passed no `avoid`, so Y's and Z's tools were
        # offered anyway. What the person said about WHICH app must not depend on the model
        # remembering to repeat it.
        said_avoid, said_asked = _split_instruction(_persons_words(ctx), apps)[1:]
        left_out |= said_avoid
        asked |= said_asked
        for given in avoid or []:
            found = _resolve(str(given), apps)
            if found:
                left_out.add(found)
        # What the person asked for by name may be an app, or only a provider or MODEL that some
        # app offers (a model catalog lives behind the app's own tools, so nothing here can know
        # whether it is there). An app is preferred outright; anything else is looked for in the
        # tools' own text, and when no tool mentions it that is said, not papered over.
        requested = str(prefer or "").strip()
        requested_app = _resolve(requested, apps) if requested else None
        if requested_app and requested_app not in left_out:
            asked.add(requested_app)
        needle = " ".join(_WORD.findall(requested.lower())) if requested and not requested_app else ""
        asked -= left_out
        # The words that NAME an app are not part of what is wanted: left in, a service
        # named only to be avoided would be the strongest match of all.
        naming = ({w for app in (left_out | asked) for w in app.split("_")}
                  | {app.replace("_", "") for app in (left_out | asked)}
                  | set(needle.split()))
        terms = [t for t in _terms(text) if t not in naming]
        if not terms and not asked:
            # Same shape as "nothing matched", deliberately: one result shape
            # means no caller has to special-case an empty search.
            return {"ok": False, "found": [], "unlock": [],
                    "error": "Say in a few words what's needed — a verb helps."}
        candidates = [s for s in searchable if _app_of(s) not in left_out]
        weights = _weights(candidates, terms)
        # A result must speak to at least 60% of what was asked. Without it, taking the
        # obvious answer away (an app left out) filled the list with one-word accidents —
        # a project lister for "generate an image" — instead of saying nothing fits.
        # Only words that SOME capability says anything about count. What a picture is of
        # ("mountain", "sunrise") is in no tool's text and never will be: it is the payload of the
        # request, not something a tool is expected to cover. Counted, it made a plain "generate an
        # image of a mountain at sunrise" match nothing at all — and the model, told "nothing",
        # searched again app by app. A request made only of such words still matches nothing.
        texts = [(s_.name.lower().replace("_", " "), (s_.description or "").lower()) for s_ in candidates]
        wanted = sorted(t for t in set(terms) if any(t in n_ or t in d_ for n_, d_ in texts))
        need = max(1, math.ceil(len(wanted) * 0.6))

        def covers(spec: CapabilitySpec) -> int:
            name = spec.name.lower().replace("_", " ")
            description = (spec.description or "").lower()
            return sum(1 for t in wanted if t in name or t in description)

        scored = [(spec, _score(spec, terms, weights)) for spec in candidates
                  if _app_of(spec) in asked or covers(spec) >= need]
        holders: set[str] = set()
        unmatched: list[str] = []
        if needle:
            holders = {spec.name for spec in candidates if needle in _spec_text(spec)}
            if not holders:
                unmatched = [requested]
        # An app asked for by name, or one whose tools mention the model that was asked for, is
        # boosted — for what it can DO about the request. A tool that merely mentions the model
        # (a "manage references" tool) must not outrank the one that generates.
        holder_apps = {a_ for a_ in (_app_of(spec) for spec in candidates if spec.name in holders) if a_}
        boost_apps = asked | holder_apps
        boost = {s_.name for s_, _ in scored if _app_of(s_) in boost_apps}
        if boost:
            bonus = max((n for _, n in scored), default=0) + 1
            scored = [(s_, n + bonus if s_.name in boost else n) for s_, n in scored]

        # Each app is judged on its own best match: one app with thirty tools must not set a bar
        # that hides another app's single, equally good one.
        by_app: dict[str, list[tuple[CapabilitySpec, float]]] = {}
        for spec, n in scored:
            by_app.setdefault(_app_of(spec) or "", []).append((spec, n))
        qualified: dict[str, list[tuple[CapabilitySpec, float]]] = {}
        for app, rows in by_app.items():
            best_here = max(n for _, n in rows)
            if best_here < 2:
                continue  # a hard floor: one incidental word in a long description is no match
            qualified[app] = sorted(((s_, n) for s_, n in rows if n >= max(2, best_here / 2)),
                                    key=lambda pair: (-pair[1], pair[0].name))
        # The best tool of EVERY capable app first, then the rest by score, at most PER_APP from
        # one app: otherwise the app with the most tools fills the whole answer and the others
        # are never even seen.
        order = sorted(qualified, key=lambda app: (-qualified[app][0][1], app))
        matches = [qualified[app][0] for app in order][:MAX_MATCHES]
        leftovers = sorted(((s_, n) for app in order for s_, n in qualified[app][1:PER_APP]),
                           key=lambda pair: (-pair[1], pair[0].name))
        matches += leftovers[:MAX_MATCHES - len(matches)]
        matches.sort(key=lambda pair: (-pair[1], pair[0].name))
        capable = sorted(app for app in qualified if "__" in (qualified[app][0][0].name))

        unusable = _named_but_unusable(terms + sorted(naming))
        notes: list[str] = []
        if left_out:
            notes.append("As asked, left out: " + ", ".join(sorted(a_.replace("_", " ") for a_ in left_out))
                         + ". Do not do this through those apps; if nothing else fits, say so.")
        if unmatched:
            notes.append(
                f"The user asked for {unmatched[0]}, and no connected app is named that or mentions it. "
                "It may still be one of the models an app below offers: look at that app's own list "
                "of models before saying otherwise. If it is not there, say so plainly and ask before "
                "using anything else — do not quietly substitute.")
        if holders:
            names_ = sorted({_app_of(spec) or spec.name for spec in candidates if spec.name in holders})
            notes.append(f"{requested} is mentioned by the tools of: {', '.join(n_.replace('_', ' ') for n_ in names_)}.")
        if len(capable) > 1:
            notes.append("More than one connected app can do this: "
                         + ", ".join(c_.replace("_", " ") for c_ in capable)
                         + ". If the user did not choose, pick the ONE best fit for what they asked and "
                           "use exactly one, and say which you used — not several at once, which would "
                           "do and charge for the same thing repeatedly — unless the user asked to "
                           "compare or to use more than one.")
        if not matches:
            # An honest "no" beats offering something unrelated (§45).
            answer: dict[str, Any] = {"ok": True, "found": [], "unlock": [],
                                      "note": f"I have nothing that does that ({intent})."}
            if notes:
                answer["note"] = " ".join(notes) + " " + answer["note"]
            if unmatched:
                answer["unmatched"] = unmatched
            if unusable:
                answer.update(notConnected=unusable, note=_unusable_note(unusable))
            return answer
        answer = {
            "ok": True,
            "found": [{"name": s_.name, "description": _brief(s_.description)} for s_, _ in matches],
            "unlock": [s_.name for s_, _ in matches],
            "note": " ".join(notes + ["These are callable now, in this same turn."]),
        }
        if len(capable) > 1:
            answer["options"] = capable
        if unmatched:
            answer["unmatched"] = unmatched
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
                     "same turn. If the user named an app to use, or not to use, pass it as "
                     "prefer or avoid — their instruction decides, not what scores highest."),
        input_schema={"type": "object", "properties": {
            "intent": {"type": "string",
                       "description": 'A few words, e.g. "schedule a reminder".'},
            "prefer": {"type": "string",
                       "description": "An app, provider or model the user asked you to use, by name, "
                                      "if they named one."},
            "avoid": {"type": "array", "items": {"type": "string"},
                      "description": "Apps the user told you NOT to use, by name, if they did."}},
            "required": ["intent"]},
        risk=Risk.LOW,
        handler=_run,
        wants_context=True,
        timeout_s=5.0,
        tags=frozenset({"core"}),
    )]
