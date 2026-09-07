"""Turning a fetched web page into readable text.

A leaf: the page reader tool and the research module both need this, and putting
it in either would make the other depend on a tool, which is the wrong direction.

Not a parser and not trying to be. Scripts and styles are dropped, tags are
stripped, entities are decoded, whitespace is collapsed. A page this cannot read
usefully is a page for a real browser, not a reason to add a parsing dependency
to a five-dependency project.
"""

from __future__ import annotations

import html
import re

_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


def to_text(markup: str) -> str:
    without_code = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", markup)
    blocks = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li|/tr)\s*/?>", "\n", without_code)
    text = re.sub(r"(?s)<[^>]+>", " ", blocks)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def title_of(markup: str) -> str | None:
    match = _TITLE.search(markup)
    return html.unescape(match.group(1)).strip() if match else None
