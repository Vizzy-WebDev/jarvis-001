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
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_OG_TITLE = re.compile(r"""(?i)<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']""")

#: A plain browser User-Agent. Several sites serve a stripped-down or
#: consent-gated page to anything that announces itself as a bot, and the
#: difference shows up as "that page had no readable text".
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

#: Generous: content analysis wants the whole article, not a taster. A caller
#: with less room passes its own lower cap.
ARTICLE_MAX_CHARS = 24000
FETCH_TIMEOUT_S = 20.0


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


@dataclass(frozen=True)
class Article:
    ok: bool
    url: str | None = None
    title: str | None = None
    text: str = ""
    error: str | None = None


def fetch_article(url: str, *, max_chars: int = ARTICLE_MAX_CHARS) -> Article:
    """A page's readable text, or a plain reason it could not be read.

    Never raises: every caller here is deciding what to tell the user, and an
    exception at this level turns "that page wouldn't load" into a failed turn.
    """
    import httpx

    target = (url or "").strip()
    if not re.match(r"^https?://", target, re.I):
        return Article(ok=False, error=f'"{url}" is not a web address I can read.')

    try:
        with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True,
                          headers={"User-Agent": BROWSER_UA}) as client:
            response = client.get(target)
            if response.status_code >= 400:
                return Article(ok=False, error=f"That page returned an error ({response.status_code}).")
            content_type = response.headers.get("content-type", "")
            if not any(kind in content_type for kind in ("text/html", "text/plain", "xml")):
                return Article(ok=False, error="That link doesn't point at a readable page.")
            markup = response.text
    except Exception as err:  # noqa: BLE001 — every transport failure reads the same here
        logger.info("could not fetch %s: %s", target, err)
        return Article(ok=False, error="Couldn't reach that page.")

    text = to_text(markup)[:max_chars]
    if not text:
        return Article(ok=False, error="That page had no readable text on it.")
    return Article(ok=True, url=str(target), title=article_title(markup), text=text)


def article_title(markup: str) -> str | None:
    """The page's own title, preferring the one it publishes for sharing — that
    is the one written for a human, where <title> often carries site furniture."""
    og = _OG_TITLE.search(markup)
    if og:
        return html.unescape(og.group(1)).strip()
    return title_of(markup)
