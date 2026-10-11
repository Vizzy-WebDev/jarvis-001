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

**A lookup has its own deadline, and what it found is never thrown away.** Measured on the
owner's install, 31 of 158 lookups ended as "didn't finish within 90s" and returned nothing:
the pages were read one after another (up to 12s each) and then a model was asked to
summarise with no limit on how long that could take. Now the pages are read at the same
time, the whole lookup works to `BUDGET_S`, and when the summary cannot be had — no model
could answer, or it would not come in time — the result is the relevant part of each page
itself (`via: "web-excerpts"`), which the model that asked can read as well as any summary.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from jarvis import tls

from .ai import NoModelAvailable, ask
from .webtext import to_text

logger = logging.getLogger(__name__)

MAX_SOURCES = 4
#: Below this much fetched text, the free path is "thin" and worth escalating.
THIN_CHARS = 600
FETCH_TIMEOUT_S = 12.0
#: How long one lookup may take in all — under `look_it_up`'s own timeout, so a lookup that
#: runs short of time still hands back what it read instead of being cut off with nothing.
BUDGET_S = 75.0
#: The least time the summarising call is given, however long the reading took.
MIN_SUMMARY_S = 10.0
#: How much of each page is handed back when there is no summary.
EXCERPT_CHARS = 1500
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
    via: str = "web"                 # 'web' | 'web-excerpts' | 'model-search'
    sources: list[Source] = field(default_factory=list)
    error: str | None = None
    query: str = ""
    #: Why there is no summary, when the pages themselves are the result (`web-excerpts`).
    note: str | None = None
    #: Which model actually answered. Recorded because a plan built by a weak
    #: fallback should not be indistinguishable from one built by the good model.
    model_id: str | None = None

    @property
    def summarised(self) -> bool:
        """True when there is a written answer — what a caller that wants prose to keep (a
        project's research note, an analysis) must check, rather than `ok`: page excerpts are a
        fine result for a model mid-turn and no use as a finished document."""
        return self.ok and bool(self.answer)

    def as_result(self) -> dict[str, Any]:
        excerpts = self.via == "web-excerpts"
        return {
            "ok": self.ok,
            "answer": self.answer,
            "via": self.via,
            "query": self.query,
            "sources": [{"title": s.title, "url": s.url,
                         **({"excerpt": s.text[:EXCERPT_CHARS]} if excerpts and s.text else {})}
                        for s in self.sources],
            **({"note": self.note} if self.note else {}),
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
            with httpx.Client(verify=tls.context(), timeout=FETCH_TIMEOUT_S, follow_redirects=True,
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
        with httpx.Client(verify=tls.context(), timeout=FETCH_TIMEOUT_S, follow_redirects=True,
                          headers={"User-Agent": BROWSER_UA}) as client:
            response = client.get(source.url)
            response.raise_for_status()
            source.text = to_text(response.text)[:8000]
    except Exception as err:  # noqa: BLE001
        logger.info("could not read %s: %s", source.url, err)
    return source


def fetch_all(sources: list[Source]) -> list[Source]:
    """Read every source at the same time: four pages cost the slowest one, not their sum."""
    if len(sources) < 2:
        return [fetch_source(s) for s in sources]
    with ThreadPoolExecutor(max_workers=len(sources), thread_name_prefix="research-fetch") as pool:
        return list(pool.map(fetch_source, sources))


def _synthesise_within(question: str, sources: list[Source],
                       seconds: float) -> tuple[str, str | None, str | None]:
    """(answer, model id, why not). The summary, if it comes within `seconds`; otherwise why
    there is none. The call itself cannot be stopped, so it is left to finish unheard."""
    box: dict[str, Any] = {}

    def go() -> None:
        try:
            box["answer"], box["model"] = _synthesise(question, sources)
        except NoModelAvailable as err:
            box["why"] = f"I couldn't summarise them: {err}"
        except Exception as err:  # noqa: BLE001 — the pages are still worth handing back
            logger.exception("summarising the sources failed")
            box["why"] = f"I couldn't summarise them ({err.__class__.__name__})."

    thread = threading.Thread(target=go, name="research-summary", daemon=True)
    thread.start()
    thread.join(max(0.0, seconds))
    if thread.is_alive():
        return "", None, "The summary was taking too long, so here is what the pages say."
    if box.get("answer"):
        return box["answer"], box.get("model"), None
    return "", None, box.get("why") or "The summary came back empty."


def _synthesise(question: str, sources: list[Source]) -> tuple[str, str | None]:
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
    data_class="personal", task_class="research")
    return answer.text.strip(), answer.model_id


# --- model-native search -----------------------------------------------------

def _model_search(question: str) -> Research:
    """The escalation that isn't available: searching the web THROUGH a model.

    Provider-hosted search is outside what the model layer does, so this says so
    plainly instead of asking a plain model — which would answer from memory as
    though it had searched.
    """
    return Research(ok=False, via="model-search", query=question,
                    error="I couldn't look that up: searching the web through the model isn't something "
                          "this setup can do, so it wasn't done.")


# --- the entry point ---------------------------------------------------------

def research(question: str, search_query: str | None = None, *,
             budget_s: float = BUDGET_S) -> Research:
    """Look something up. Free path first, model-native search only if it is thin."""
    asked = (question or "").strip()
    if not asked:
        return Research(ok=False, error="There's nothing to look up.")

    started = time.monotonic()
    query = (search_query or to_search_query(asked)).strip()
    sources = fetch_all(search(query))
    readable = [s for s in sources if s.text]
    total = sum(len(s.text) for s in readable)

    if readable and total >= THIN_CHARS:
        left = budget_s - (time.monotonic() - started)
        answer, model_id, why_not = _synthesise_within(asked, readable, max(MIN_SUMMARY_S, left))
        if answer:
            return Research(ok=True, answer=answer, via="web", sources=readable, query=query,
                            model_id=model_id)
        # The pages were read; only the summarising failed or ran out of time. What they say
        # is the result: whoever asked can read it as well as a summary.
        return Research(ok=True, via="web-excerpts", query=query, sources=readable, note=why_not)

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
