"""Top news headlines, from Google News' public RSS feed."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..capabilities import CapabilitySpec, Risk
from ._http import get_text

FEED_URL = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"


def _titles(xml: str, limit: int) -> list[str]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if title:
            out.append(re.sub(r"\s+", " ", title))
        if len(out) >= limit:
            break
    return out


def _run(topic: str | None = None, count: int = 5) -> dict:
    limit = max(1, min(10, int(count or 5)))
    url = (f"https://news.google.com/rss/search?q={quote(topic)}&hl=en-US&gl=US&ceid=US:en"
           if topic else FEED_URL)
    try:
        headlines = _titles(get_text(url), limit)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "Couldn't reach the news service."}
    if not headlines:
        return {"ok": False, "error": "No headlines found."}
    return {"ok": True, "topic": topic or "top stories", "headlines": headlines}


SPEC = CapabilitySpec(
    id="builtin.get_headlines",
    name="get_headlines",
    description=("Get current news headlines, optionally about a topic. Use this when the user "
                 "asks what's happening or wants the news."),
    input_schema={"type": "object", "properties": {
        "topic": {"type": "string", "description": 'Optional topic, e.g. "space" or "Arsenal".'},
        "count": {"type": "integer", "description": "How many headlines. Default 5, max 10."}},
        "required": []},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=20.0,
)
