"""Read a web page the user already has a URL for, as plain text.

Deliberately does NOT open a visible browser window: a plain read is invisible
work. The browser is for pages that must genuinely be clicked or typed into.
"""

from __future__ import annotations

import html
import re

from ..capabilities import CapabilitySpec, Risk
from ._http import get_text

MAX_CHARS = 20000


def to_text(markup: str) -> str:
    """A small, dependency-free HTML-to-text pass.

    Not a parser and not trying to be: scripts and styles are dropped, tags are
    stripped, entities are decoded and whitespace is collapsed. A page this
    cannot read usefully is a page for the browser connector, not a reason to
    add a parsing dependency.
    """
    without_code = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", markup)
    blocks = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li|/tr)\s*/?>", "\n", without_code)
    text = re.sub(r"(?s)<[^>]+>", " ", blocks)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _title(markup: str) -> str | None:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", markup)
    return html.unescape(match.group(1)).strip() if match else None


def _run(url: str = "") -> dict:
    target = str(url or "").strip()
    if not re.match(r"^https?://", target, re.I):
        return {"ok": False, "error": "That doesn't look like a web address."}
    try:
        markup = get_text(target)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f"Couldn't read that page ({err.__class__.__name__})."}
    text = to_text(markup)
    if not text:
        return {"ok": False, "error": "That page had no readable text — it may need a browser."}
    return {"ok": True, "url": target, "title": _title(markup),
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
    timeout_s=25.0,
)
