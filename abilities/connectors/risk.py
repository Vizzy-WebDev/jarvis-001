"""How dangerous is a tool nobody here declared?

Every built-in capability states its own risk, which is what makes the
permission decision independent of the model. A connector tool cannot: its name
and description come from a server this build has never seen. So risk is
inferred — and the inference is deliberately blunt and word-based rather than
clever, because the cost of being wrong is asymmetric. A false "risky" costs one
confirmation; a false "safe" sends the email.

**Whole words, never substrings.** A real MCP server's descriptions are prose,
and substring matching found "share" inside "SharePoint" and "order" inside "in
sidebar order" — both real tools, both harmless, both flagged. Tokenising means
"share" matches only the word share.

**And camelCase is split in a tool's NAME but not in its description.** The
original splits both, which quietly re-creates the very false positive the
tokeniser was added to remove: "Search SharePoint" becomes "search share point",
and the tool is risky again. A name is an identifier, where `updatePet` really
does mean update; a description is prose, where SharePoint is a proper noun.
Different shapes, different rules.

Pure: no I/O, no state, exhaustively testable as a table.
"""

from __future__ import annotations

import re

#: Words that mean an action reaches outside, spends something, or cannot be
#: taken back. `update`, `write` and `move` are in here after real tools that
#: overwrite existing content matched none of the rest — a little more
#: day-to-day confirming, in exchange for "irreversible always asks" being true.
IRREVERSIBLE = frozenset({
    "delete", "remove", "trash", "uninstall",
    "send", "email", "message", "publish", "post", "share", "tweet",
    "pay", "purchase", "buy", "checkout", "transfer", "wire",
    "install", "execute", "format", "overwrite",
    "update", "write", "move",
})

#: `order` was tried and dropped: it is common enough with an innocent meaning
#: ("in sidebar order") that it flagged a harmless real tool, and buy/purchase/
#: checkout already carry the spends-money meaning without the ambiguity.
DELIBERATELY_NOT_LISTED = frozenset({"order"})

#: Command fragments, matched as plain substrings because that is what they are.
#: Kept apart from the word list above: ordinary prose is full of "write" and
#: "update", but nothing writes "rm -rf" by accident.
DESTRUCTIVE_COMMANDS = (
    "rm -rf", "rm -r ", "sudo rm", "del /f", "del /s", "rd /s", "rmdir /s",
    "format c:", "format d:", ":(){ :|:& };:",
    "drop table", "drop database", "truncate table",
    "shutdown /s", "shutdown -s",
)

#: A tool's own documentation can run to thousands of characters with worked
#: examples. Scanning all of it finds far more incidental matches than real
#: ones, so only the opening sentence is read.
_SENTENCE = re.compile(r"^[^.!?\n]{0,300}")


def words(text: str, *, split_camel_case: bool = True) -> list[str]:
    """Whole words, splitting hyphens and underscores — and camelCase, for a name.

    The camelCase split needs the original casing, so this must not be handed an
    already-lowered string: lowercasing first makes the split silently do
    nothing.
    """
    source = str(text or "")
    if split_camel_case:
        source = re.sub(r"([a-z])([A-Z])", r"\1 \2", source)
    return [word for word in re.split(r"[^a-z]+", source.lower()) if word]


def first_sentence(text: str) -> str:
    match = _SENTENCE.match(str(text or "").strip())
    return match.group(0) if match else ""


def classify(name: str, description: str = "") -> str:
    """'safe' or 'risky' for one connector tool.

    Only two levels on purpose. A third would need a rule for what to do with
    it, and every honest answer to "somewhat risky" is one of the two already
    here.
    """
    text = str(name or "").lower()
    if any(fragment in text for fragment in DESTRUCTIVE_COMMANDS):
        return "risky"

    tokens = (set(words(name))
              | set(words(first_sentence(description), split_camel_case=False)))
    return "risky" if tokens & IRREVERSIBLE else "safe"
