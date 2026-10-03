"""Open a web page, or a search for one, in the user's own browser.

LOW risk under §7's own examples ("open an application"). It shows the user
something; it changes nothing.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from ..capabilities import CapabilitySpec, Risk
from ._http import open_in_browser


def _open(url: str, **extra) -> dict:
    opened = open_in_browser(url)
    # Honest either way: on a machine with no desktop session the link is real
    # but nothing opened, and saying "opened it" there would be a fake success.
    return {"ok": True, "url": url, "opened": opened,
            **({} if opened else {"note": "I couldn't open a browser here — here's the link."}),
            **extra}


def _run_open(url: str = "") -> dict:
    target = str(url or "").strip()
    if not target:
        return {"ok": False, "error": "No address given."}
    if not re.match(r"^https?://", target, re.I):
        target = f"https://{target}"
    return _open(target)


def _run_search(query: str = "") -> dict:
    text = str(query or "").strip()
    if not text:
        return {"ok": False, "error": "No search query given."}
    # Percent-encoded, and passed as a single argument — never interpolated into
    # a shell string.
    return _open(f"https://www.google.com/search?q={quote(text)}", query=text)


SPECS = [
    CapabilitySpec(
        id="builtin.open_website", name="open_website",
        description="Open a website in the user's browser.",
        input_schema={"type": "object", "properties": {
            "url": {"type": "string", "description": "The address to open."}},
            "required": ["url"]},
        risk=Risk.LOW, handler=_run_open, timeout_s=10.0, tags=frozenset({"core"}),
    ),
    CapabilitySpec(
        id="builtin.web_search", name="web_search",
        description=("Open a web search and show the user the results. Use this when they ask "
                     "you to look something up in their browser rather than read it yourself."),
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": 'What to search for.'}},
            "required": ["query"]},
        risk=Risk.LOW, handler=_run_search, timeout_s=10.0, tags=frozenset({"core"}),
    ),
]
