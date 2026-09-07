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
from jarvis.gateway import availability, connections, registry
from jarvis.research import Source, research, search, to_search_query

from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    availability.reset_for_tests()
    yield
    availability.reset_for_tests()
    reset_db()


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    conn = connections.add_connection(adapter="openai-compatible", base_url=server.base_url,
                                      label="stub", provider="custom", kind="local",
                                      key_required=False)
    registry.add_model(connection_id=conn["id"], model="stub-model")
    yield server
    server.stop()


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

def test_readable_sources_are_answered_from_the_web(stub, monkeypatch):
    monkeypatch.setattr(research_module, "search",
                        lambda q: [Source("A page", "https://example.com/a")])
    monkeypatch.setattr(research_module, "fetch_source",
                        lambda s: Source(s.title, s.url, "Lagos has a population of many. " * 40))
    stub.says("Lagos is large.")

    result = research("how big is Lagos")
    assert result.ok and result.via == "web"
    assert result.answer == "Lagos is large."
    assert [s.url for s in result.sources] == ["https://example.com/a"]


def test_a_thin_web_result_escalates_to_a_model_that_can_search(stub, monkeypatch):
    """The order is deliberate: on a free tier, one provider request is a
    meaningful slice of a day."""
    # A model only qualifies if it actually declares web search. Setting that
    # here also exercises the §26 fix: capabilities are a model's own overridable
    # facts, not a hard ceiling imposed by whichever adapter it happens to use.
    searcher = registry.list_models()[0]
    registry.update_model(searcher["id"], {"caps": {**searcher["caps"], "webSearch": True}})
    monkeypatch.setattr(research_module, "search", lambda q: [])
    stub.says("I looked it up: it's large.")

    result = research("how big is Lagos")
    assert result.ok and result.via == "model-search"


def test_nothing_escalates_to_a_model_that_cannot_search(stub, monkeypatch):
    """The honest outcome on a roster where nothing declares web search: say so,
    rather than asking a plain model to pretend it looked."""
    monkeypatch.setattr(research_module, "search", lambda q: [])
    result = research("how big is Lagos")
    assert result.ok is False
    assert "no model here can search" in result.error


def test_when_both_paths_fail_it_says_which(monkeypatch):
    monkeypatch.setattr(research_module, "search", lambda q: [])
    result = research("how big is Lagos")     # no model configured at all
    assert result.ok is False
    assert "couldn't find anything useful" in result.error


def test_sources_survive_a_failed_summary(stub, monkeypatch):
    """The pages were fetched; only the summarising failed. They are still worth
    handing back."""
    monkeypatch.setattr(research_module, "search",
                        lambda q: [Source("A page", "https://example.com/a")])
    monkeypatch.setattr(research_module, "fetch_source",
                        lambda s: Source(s.title, s.url, "Plenty of real text here. " * 40))
    stub.fails(429, "Rate limit exceeded")
    stub.fails(429, "Rate limit exceeded")

    result = research("how big is Lagos")
    assert result.ok is False and result.sources
    assert "couldn't" in result.error


def test_nothing_to_look_up_is_refused_without_a_search():
    assert research("   ").ok is False
