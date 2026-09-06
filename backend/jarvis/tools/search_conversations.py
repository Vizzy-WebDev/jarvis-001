"""Search everything that has actually been said, across every conversation.

Distinct from recalling a FACT about the user, which needs no search at all —
the approved memory set is small enough to sit in the prompt. This is for
recalling something SAID, where the date matters: a past statement is not
automatically still true, so every result carries when it was said.
"""

from __future__ import annotations

from .. import chat_store
from ..capabilities import CapabilitySpec, Risk

MAX_RESULTS = 8


def _run(query: str = "", limit: int = MAX_RESULTS) -> dict:
    text = str(query or "").strip()
    if not text:
        return {"ok": False, "error": "No search text given."}
    rows = chat_store.search_messages(text, limit=max(1, min(20, int(limit or MAX_RESULTS))))
    if not rows:
        return {"ok": True, "query": text, "results": [],
                "note": "Nothing in past conversations matches that."}
    return {"ok": True, "query": text, "results": rows,
            "note": ("These are things that were SAID, with dates. Say when something was said "
                     "rather than asserting it is still true.")}


SPEC = CapabilitySpec(
    id="builtin.search_conversations",
    name="search_conversations",
    description=("Search past conversations for something that was said. Use this when the user "
                 "refers to an earlier discussion — 'what did I say about...', 'when did we "
                 "talk about...'."),
    input_schema={"type": "object", "properties": {
        "query": {"type": "string", "description": "Words to search for."},
        "limit": {"type": "integer", "description": "How many results. Default 8."}},
        "required": ["query"]},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=10.0,
)
