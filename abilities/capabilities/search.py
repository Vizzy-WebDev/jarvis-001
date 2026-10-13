"""Finding a capability by what is wanted, in plain words.

Most of what Jarvis can do is not declared to the model on every turn (measured: declaring
everything cost ~150,000 characters on "hello"). Two things reach the rest, and both rank with
the functions here so they can never disagree about what "matches" means:

- `find_capability` (a tool the model calls) — the model has to THINK to call it.
- the turn loop itself (`relevant()`), which looks up what the person's own words point at
  before the model is asked anything, and declares those too. Found by a live run: asked to
  "add up the March invoices in the office records", a real model never searched — it tried a
  file tool it could see, was refused, and asked the person where the records were. The tools
  it needed existed and matched its request word for word. Looking them up is plain code; it
  should not cost a model step, and it should not depend on the model deciding to look.

Pure: no I/O, no registry of its own — it is handed the specs to rank.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

_WORD = re.compile(r"[a-z0-9]+")

#: Filler that appears in nearly every capability description, so matching on it
#: returns the whole registry — which is the same as returning nothing useful.
STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "your", "you", "this", "that", "what",
    "when", "how", "can", "use", "user", "asks", "about", "them", "something",
    "anything", "please", "want", "wants", "need", "needs", "help", "me", "my",
    "which", "there", "their", "its", "was", "were", "are", "into", "then", "tell",
    "all", "any", "each", "every", "out", "one", "get", "let", "know", "done", "work",
})

MAX_MATCHES = 6

#: For what the turn loop declares unasked (`relevant`): the whole lot may not weigh more than
#: this many characters, and a single word of the request is only enough when at most this
#: many capabilities carry it in their name.
RELEVANT_MAX_CHARS = 9000
RARE_NAME_WORD = 2
#: Verbs nearly any request uses. One of them alone never picks a capability, however few
#: names carry it ("Start at the record…" is not a request to start a project).
GENERIC_VERBS = frozenset({"start", "stop", "open", "make", "run", "read", "list", "check", "send",
                           "get", "set", "add", "show", "find", "create", "update", "delete",
                           "remove", "write", "save", "ask", "tell", "give", "take", "put"})


def terms(text: str) -> list[str]:
    """The words worth matching on, each once, in the order said."""
    out: list[str] = []
    for word in _WORD.findall((text or "").lower()):
        if len(word) > 2 and word not in STOPWORDS and word not in out:
            out.append(word)
    return out


def _stem(word: str) -> str:
    """"invoices" finds `invoice`, "records" finds `record`: a plural is the same thing."""
    for suffix, minimum in (("ies", 5), ("es", 5), ("s", 4)):
        if len(word) >= minimum and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def score(spec: Any, wanted: list[str]) -> int:
    """3 for each wanted word in the capability's name, 1 for each in its description."""
    name = spec.name.lower().replace("_", " ")
    description = (spec.description or "").lower()
    total = 0
    for term in wanted:
        stem = _stem(term)
        if term in name or stem in name:
            total += 3
        if term in description or stem in description:
            total += 1
    return total


def matches(specs: Iterable[Any], text: str, *, limit: int = MAX_MATCHES,
            min_score: int = 2) -> list[tuple[Any, int]]:
    """The capabilities that fit `text`, best first, as (spec, score).

    Two gates, both needed: a hard floor so one incidental word in a long description is not
    a match, and a relative floor so a genuinely strong match is not diluted by weak ones.
    """
    wanted = terms(text)
    if not wanted:
        return []
    scored = [(spec, score(spec, wanted)) for spec in specs]
    best = max((n for _, n in scored), default=0)
    floor = max(min_score, best / 2)
    return sorted(((s, n) for s, n in scored if n >= floor),
                  key=lambda pair: (-pair[1], pair[0].name))[:limit]


def _name_words(spec: Any) -> set[str]:
    return {_stem(word) for word in _WORD.findall(spec.name.lower().replace("_", " "))}


def relevant(specs: Iterable[Any], text: str, *, among: Iterable[Any] | None = None,
             limit: int = MAX_MATCHES) -> list[str]:
    """Names of the capabilities (of `specs`) a request plainly points at — what the turn loop
    declares without being asked. Much stricter than a search someone asked for, because it
    runs on every turn and whatever it returns costs prompt space on every step:

    - only whole words of a capability's NAME count ("file" is not in `profile`);
    - two different words of the request must be in the name ("office" + "records"), or one
      word that hardly any capability carries ("look" → `look_it_up`) and that is not a verb
      every request uses. A word many names share ("check", "file", "task") says nothing by
      itself: found by a test, "Check the files" pulled in six unrelated tools on a looser rule.

    `among` is every capability there is (default: `specs`), which is what "hardly any" is
    counted over — a word is not rare because the others carrying it were left out of `specs`.
    """
    specs = list(specs)
    wanted = [_stem(term) for term in terms(text)]
    if not wanted:
        return []
    words = {spec.name: _name_words(spec) for spec in specs}
    everywhere = [_name_words(spec) for spec in (among if among is not None else specs)]
    carried = {term: sum(1 for held in everywhere if term in held) for term in set(wanted)}
    ranked = []
    for spec in specs:
        hits = [term for term in dict.fromkeys(wanted) if term in words[spec.name]]
        alone = (len(hits) == 1 and hits[0] not in GENERIC_VERBS
                 and carried[hits[0]] <= RARE_NAME_WORD)
        if len(hits) >= 2 or alone:
            ranked.append((len(hits), score(spec, wanted), spec))
    ranked.sort(key=lambda row: (-row[0], -row[1], row[2].name))
    names: list[str] = []
    weight = 0
    for _, _, spec in ranked:
        size = len(spec.description or "") + len(json.dumps(spec.input_schema or {}, default=str))
        if len(names) >= limit or weight + size > RELEVANT_MAX_CHARS:
            continue
        names.append(spec.name)
        weight += size
    return names
