"""Read a web page the user already has a URL for, as plain text.

Deliberately does NOT open a visible browser window: a plain read is invisible
work. Three levels, and it only goes down one when the level above genuinely
came back with nothing: a plain fetch, then a headless render for a page that
builds itself in the browser, and — only as something to SAY, never something
this tool does on its own — the visible browser, which is for pages that have to
be clicked or typed into.

That last step is a sentence rather than a call on purpose. Opening a window on
someone's screen is a thing they should have asked for; a lookup that quietly
pops one up is the behaviour this ordering exists to prevent.
"""

from __future__ import annotations

import re
import time

from ..capabilities import CapabilitySpec, Risk
from ..webtext import title_of, to_text
from ._http import get_text

MAX_CHARS = 20000
#: How long one read may take in all, and the least a headless render is worth starting with.
#: Measured on the owner's install, 17 of 118 reads ended as "didn't finish within 25s": a
#: plain fetch may take 15s and the render after it 25s more, so the tool's old 25s limit cut
#: off the very reads that needed the render — and threw away the page the fetch HAD read.
BUDGET_S = 45.0
MIN_RENDER_S = 8.0
#: Below this, what came back is almost certainly a page shell rather than an
#: article — worth rendering properly before reporting there is nothing there.
THIN_CHARS = 600
def _run(url: str = "") -> dict:
    target = str(url or "").strip()
    if not re.match(r"^https?://", target, re.I):
        return {"ok": False, "error": "That doesn't look like a web address."}
    started = time.monotonic()
    try:
        markup = get_text(target)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f"Couldn't read that page ({err.__class__.__name__})."}
    text = to_text(markup)
    rendered = False
    left = BUDGET_S - (time.monotonic() - started)
    if len(text) < THIN_CHARS and left >= MIN_RENDER_S:
        # Almost nothing came back. That is usually a page that builds itself in
        # the browser rather than a page with nothing on it, so it is worth
        # actually running — headlessly, invisibly. A plain fetch that came back
        # thin and an empty page look identical from here, and answering from
        # the shell of a page is worse than taking the extra second.
        from ..webrender import render

        # Within what is left of this read's own time, less the few seconds a page gets to settle.
        attempt = render(target, timeout_ms=int(max(MIN_RENDER_S - 5, left - 7) * 1000))
        if attempt.ok:
            fuller = to_text(attempt.html)
            if len(fuller) > len(text):
                markup, text, rendered = attempt.html, fuller, True

    if not text:
        if rendered:
            # It was really run, and still had nothing readable. What is left is
            # a page that needs interacting with — which is the browser
            # connector's job, and the user's call to make.
            return {"ok": False,
                    "error": "That page had no readable text even after running it. If it "
                             "needs signing in or clicking through, say so and I can open "
                             "it in a browser window you can watch."}
        return {"ok": False,
                "error": "That page had no readable text — it may need a real browser."}
    return {"ok": True, "url": target, "title": title_of(markup), "rendered": rendered,
            "truncated": len(text) > MAX_CHARS, "text": text[:MAX_CHARS]}


SPEC = CapabilitySpec(
    id="builtin.read_web_page",
    name="read_web_page",
    description=("Read the text of a web page you already have the address for. Use this to "
                 "look something up quietly rather than opening a browser window."),
    input_schema={"type": "object", "properties": {
        "url": {"type": "string", "description": "The full web address, starting with http."}},
        "required": ["url"]},
    risk=Risk.LOW,
    handler=_run,
    # Above the read's own deadline (`BUDGET_S`), which is what normally ends it.
    timeout_s=55.0,
    # `core`, like look_it_up: the second most used ability (118 calls).
    tags=frozenset({"core"}),
)
