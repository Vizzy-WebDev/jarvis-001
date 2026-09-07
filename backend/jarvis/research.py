"""Actually looking something up, rather than answering from memory.

Two backends, cheapest first:

1. **The free path** — search over plain HTTP, fetch the top few pages, and make
   ONE model call to synthesise an answer with its sources attached. No search
   key, no search quota.
2. **Model-native search** — hand the question to a model whose adapter can
   search for itself. Better on obscure or very recent questions, and only used
   when the free path came back thin.

That order is deliberate on a free tier, where one provider request can be a
meaningful slice of a day's allowance. `via` is returned and shown to the user:
where an answer came from changes what it is worth.

**Search with keywords, not with the question.** A raw question can return
nothing where the same subject reduced to keywords returns dozens of results;
`to_search_query()` is that reduction, and it is not cosmetic.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from .gateway.client import NoModelAvailable, ask
from .gateway.routing import Task
from .webtext import to_text

logger = logging.getLogger(__name__)

MAX_SOURCES = 4
#: Below this much fetched text, the free path is "thin" and worth escalating.
THIN_CHARS = 600
FETCH_TIMEOUT_S = 12.0
#: A browser UA: several of these endpoints serve an anti-bot page to anything
#: that announces itself as a script, which is a fetch that succeeds and returns
#: nothing useful — the worst kind of failure to debug.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_STOPWORDS = frozenset({
    "what", "whats", "is", "are", "the", "a", "an", "of", "to", "in", "on", "for",
    "do", "does", "did", "can", "could", "would", "should", "tell", "me", "about",
    "please", "how", "why", "when", "who", "it", "this", "that", "and", "or",
})


@dataclass
class Source:
    title: str
    url: str
    text: str = ""


@dataclass
class Research:
    ok: bool
    answer: str = ""
    via: str = "web"                 # 'web' | 'model-search'
    sources: list[Source] = field(default_factory=list)
    error: str | None = None
    query: str = ""

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "answer": self.answer,
            "via": self.via,
            "query": self.query,
            "sources": [{"title": s.title, "url": s.url} for s in self.sources],
            **({"error": self.error} if self.error else {}),
        }


def to_search_query(question: str) -> str:
    """Reduce a question to what is worth typing into a search box.

    A quoted claim inside the question is the real subject — "is it true that
    'X'" should search for X, not for the framing.
    """
    text = (question or "").strip()
    quoted = re.search(r"[\"“']([^\"”']{8,})[\"”']", text)
    if quoted:
        text = quoted.group(1)
    words = [w for w in re.findall(r"[\w'-]+", text)
             if w.lower() not in _STOPWORDS]
    # Keep the original order: a search engine reads a phrase, not a bag.
    return " ".join(words[:12]) or text[:80]


# --- the free path -----------------------------------------------------------

def _unwrap(href: str) -> str | None:
    """DuckDuckGo hands back `//duckduckgo.com/l/?uddg=<real url>` redirects.
    Unwrapping means the fetch goes to the real page, and the source list shows
    the user a real domain."""
    if not href:
        return None
    url = f"https:{href}" if href.startswith("//") else href
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    target = parse_qs(parsed.query).get("uddg", [None])[0]
    if target:
        return target
    if "duckduckgo.com" in (parsed.hostname or "") and parsed.path != "/html/":
        return None
    return url


def _useful(url: str | None) -> bool:
    if not url or not re.match(r"^https?://", url, re.I):
        return False
    # A search page or a video player fetches fine and returns navigation chrome.
    return not re.search(
        r"duckduckgo\.com|google\.[a-z.]+/search|bing\.com/search|youtube\.com/watch|\.pdf($|\?)",
        url, re.I)


#: Either quote style: the two endpoints' markup differs in ways that have
#: changed before, and a parser that only accepts one of them fails silently —
#: as zero results, which reads exactly like "the web had nothing".
_RESULT = re.compile(
    r"""<a\s+[^>]*href=(["'])((?:https?:)?//[^"']*uddg=[^"']+)\1[^>]*>([\s\S]*?)</a>""", re.I)


def search(query: str) -> list[Source]:
    """DuckDuckGo's no-JavaScript endpoints. Two of them, because the HTML one
    intermittently serves an anti-bot page and the lite one usually still
    answers.

    Matched on the `uddg=` redirect rather than a CSS class: the two endpoints
    disagree about markup but both wrap every real result that way, and it
    ignores their own navigation links, which are not wrapped.
    """
    endpoints = [
        f"https://html.duckduckgo.com/html/?q={quote(query)}",
        f"https://lite.duckduckgo.com/lite/?q={quote(query)}",
    ]
    for endpoint in endpoints:
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True,
                              headers={"User-Agent": BROWSER_UA,
                                       "Accept-Language": "en-US,en;q=0.9"}) as client:
                response = client.get(endpoint)
                if response.status_code != 200:
                    continue
                markup = response.text
        except Exception:  # noqa: BLE001
            continue

        found: list[Source] = []
        seen: set[str] = set()
        for _quote, href, label in _RESULT.findall(markup):
            url = _unwrap(html.unescape(href))
            if not _useful(url) or url in seen:
                continue
            seen.add(url)  # type: ignore[arg-type]
            title = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
            found.append(Source(title=title or url, url=url))  # type: ignore[arg-type]
            if len(found) >= MAX_SOURCES:
                break
        if found:
            return found
    return []


def fetch_source(source: Source) -> Source:
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True,
                          headers={"User-Agent": BROWSER_UA}) as client:
            response = client.get(source.url)
            response.raise_for_status()
            source.text = to_text(response.text)[:8000]
    except Exception as err:  # noqa: BLE001
        logger.info("could not read %s: %s", source.url, err)
    return source


def _synthesise(question: str, sources: list[Source]) -> str:
    body = "\n\n".join(f"[{i + 1}] {s.title} — {s.url}\n{s.text}"
                       for i, s in enumerate(sources) if s.text)
    answer = ask(
        "Answer the question using only the sources below. Say plainly when they "
        "do not actually answer it — an honest 'the sources don't say' is a "
        "better answer than a confident guess.\n\n"
        f"Question: {question}\n\nSources:\n{body}",
        system=("You answer questions from supplied sources. You never assert "
                "anything the sources do not support, and you say when they are "
                "silent or disagree."),
        task=Task(text=question, needs_tools=False, background=True),
    )
    return answer.text.strip()


# --- model-native search -----------------------------------------------------

def _model_search(question: str) -> Research:
    """The escalation: a model that can search for itself.

    Not a separate code path so much as a different prompt — the gateway picks a
    candidate that declares `webSearch`, and if none does, this fails honestly
    rather than pretending a plain model searched anything.
    """
    try:
        answer = ask(
            f"Look this up and answer it: {question}",
            system="You research questions and answer from what you find, citing where.",
            task=Task(text=question, needs_tools=False, need={"webSearch": True}),
        )
    except NoModelAvailable as err:
        return Research(ok=False, via="model-search", query=question,
                        error=f"I couldn't look that up: {err}")
    return Research(ok=True, answer=answer.text.strip(), via="model-search", query=question)


# --- the entry point ---------------------------------------------------------

def research(question: str, search_query: str | None = None) -> Research:
    """Look something up. Free path first, model-native search only if it is thin."""
    asked = (question or "").strip()
    if not asked:
        return Research(ok=False, error="There's nothing to look up.")

    query = (search_query or to_search_query(asked)).strip()
    sources = [fetch_source(s) for s in search(query)]
    readable = [s for s in sources if s.text]
    total = sum(len(s.text) for s in readable)

    if readable and total >= THIN_CHARS:
        try:
            answer = _synthesise(asked, readable)
        except NoModelAvailable as err:
            # The pages were fetched; only the summarising failed. Say so, and
            # hand back the sources — they are still worth something.
            return Research(ok=False, via="web", query=query, sources=readable,
                            error=f"I found sources but couldn't summarise them: {err}")
        if answer:
            return Research(ok=True, answer=answer, via="web", sources=readable, query=query)

    escalated = _model_search(asked)
    if escalated.ok:
        escalated.sources = readable
        escalated.query = query
        return escalated
    # Both paths failed. Say which, rather than one generic apology.
    return Research(
        ok=False, via="web", query=query, sources=readable,
        error=("I couldn't find anything useful on the web for that, and no model "
               "here can search on its own." if not readable else escalated.error))
