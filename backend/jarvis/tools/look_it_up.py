"""Looking something up on the web, invisibly.

Distinct from `web_search`, which opens a browser window for the USER to read.
This one reads the web itself and answers, so an ordinary lookup never makes a
window appear on someone's screen.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk


def _run(question: str = "", search_terms: str | None = None) -> dict[str, Any]:
    from ..research import research    # imported here: research pulls in the gateway

    if not (question or "").strip():
        return {"ok": False, "error": "There's nothing to look up."}
    return research(question, search_query=search_terms).as_result()


SPEC = CapabilitySpec(
    id="builtin.look_it_up", name="look_it_up",
    description=("Research something on the web and answer from what you find — how something "
                 "works, background, typical costs, anything that calls for finding "
                 "information. Works invisibly; nothing opens on the user's screen."),
    input_schema={"type": "object", "properties": {
        "question": {"type": "string", "description": "What to find out."},
        "search_terms": {"type": "string",
                         "description": "Optional: the concise subject to search for, if the "
                                        "question is long or roundabout."}},
        "required": ["question"]},
    risk=Risk.LOW,
    handler=_run,
    # A search, several page fetches and a model call. The default 30s is not
    # enough, and a timeout here means the whole lookup is wasted.
    timeout_s=90.0,
)
