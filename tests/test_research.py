"""Research (the free path first, model-native search only when it is thin).

The web itself is not exercised — a test that depends on DuckDuckGo answering is
a test that fails for reasons that have nothing to do with this code. What IS
exercised: the query reduction, the result parsing against real recorded markup
shapes, the escalation rule, and every failure path saying which half failed.
"""

from __future__ import annotations

import pytest

from jarvis import research as research_module
from jarvis.db import reset_for_tests as reset_db
from jarvis.research import Source, research, search, to_search_query


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


# --- the query -------------------------------------------------------------

def test_a_question_is_reduced_to_what_is_worth_typing_into_a_search_box():
    """Not cosmetic: a raw question can return nothing where the same subject as
    keywords returns dozens of results."""
    assert to_search_query("what is the current population of Lagos") == "current population Lagos"


def test_a_quoted_claim_is_the_real_subject():
    assert to_search_query("is it true that 'the Thames froze over in 1814'") == \
        "Thames froze over 1814"


# --- parsing real result markup --------------------------------------------

def test_both_duckduckgo_layouts_parse(monkeypatch):
    """The two endpoints agree on almost nothing except the redirect wrapper,
    which is why matching keys on that rather than on a CSS class."""
    html_layout = (
        '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2F'
        'example.com%2Fa">First <b>result</b></a>')
    lite_layout = (
        "<a href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fb' class='result-link'>"
        "Second result</a>")

    for markup, host in ((html_layout, "example.com"), (lite_layout, "example.org")):
        monkeypatch.setattr(research_module.httpx, "Client", _client_returning(markup))
        results = search("anything")
        assert len(results) == 1 and host in results[0].url


def test_a_search_page_or_video_is_not_treated_as_a_readable_source(monkeypatch):
    markup = ('<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.youtube.com%2Fwatch'
              '%3Fv%3D1">A video</a>')
    monkeypatch.setattr(research_module.httpx, "Client", _client_returning(markup))
    assert search("anything") == []


class _client_returning:
    def __init__(self, text: str, status: int = 200):
        self._text, self._status = text, status

    def __call__(self, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, _url):
        outer = self

        class Response:
            status_code = outer._status
            text = outer._text

            def raise_for_status(self):
                pass

        return Response()


# --- the two paths ----------------------------------------------------------


def test_when_both_paths_fail_it_says_which(monkeypatch):
    monkeypatch.setattr(research_module, "search", lambda q: [])
    result = research("how big is Lagos")     # no model configured at all
    assert result.ok is False
    assert "couldn't find anything useful" in result.error


def test_nothing_to_look_up_is_refused_without_a_search():
    assert research("   ").ok is False


# --- a lookup keeps to its own time, and keeps what it found --------------------------------

PAGE = "Lagos is the most populous city in Nigeria. " * 40


def _two_sources(monkeypatch, delay: float = 0.0):
    import time

    def fetch(source):
        time.sleep(delay)
        source.text = PAGE
        return source

    monkeypatch.setattr(research_module, "search",
                        lambda q: [Source("A", "https://a.example/x"), Source("B", "https://b.example/y")])
    monkeypatch.setattr(research_module, "fetch_source", fetch)


def test_the_pages_are_read_at_the_same_time_not_one_after_another(monkeypatch):
    import time

    _two_sources(monkeypatch, delay=0.4)
    monkeypatch.setattr(research_module, "_synthesise", lambda q, s: ("About 15 million.", "m"))
    started = time.monotonic()
    result = research("how big is Lagos")
    assert result.ok and result.answer == "About 15 million." and result.via == "web"
    assert time.monotonic() - started < 0.75      # two 0.4s reads, side by side


def test_when_no_model_can_summarise_the_pages_themselves_are_the_result(monkeypatch):
    """Before: `ok: False` and two titles — the reading was thrown away with the summary."""
    _two_sources(monkeypatch)                      # no model configured at all
    result = research("how big is Lagos").as_result()
    assert result["ok"] is True and result["via"] == "web-excerpts" and result["answer"] == ""
    assert "couldn't summarise" in result["note"]
    assert [s["url"] for s in result["sources"]] == ["https://a.example/x", "https://b.example/y"]
    assert all(s["excerpt"].startswith("Lagos is the most populous city") for s in result["sources"])
    assert all(len(s["excerpt"]) <= research_module.EXCERPT_CHARS for s in result["sources"])


def test_a_summary_that_will_not_come_in_time_is_not_waited_for(monkeypatch):
    import threading
    import time

    _two_sources(monkeypatch)
    never = threading.Event()
    monkeypatch.setattr(research_module, "_synthesise", lambda q, s: (never.wait(30), ("late", "m"))[1])
    monkeypatch.setattr(research_module, "MIN_SUMMARY_S", 0.2)
    started = time.monotonic()
    result = research("how big is Lagos", budget_s=0.2).as_result()
    never.set()
    assert time.monotonic() - started < 5
    assert result["ok"] is True and result["via"] == "web-excerpts"
    assert "taking too long" in result["note"] and result["sources"][0]["excerpt"]


def test_a_summary_still_carries_no_page_text(monkeypatch):
    _two_sources(monkeypatch)
    monkeypatch.setattr(research_module, "_synthesise", lambda q, s: ("About 15 million.", "m"))
    result = research("how big is Lagos").as_result()
    assert "note" not in result and all("excerpt" not in s for s in result["sources"])
