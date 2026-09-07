"""The built-in capabilities, and the loader that registers them.

Network-touching tools are tested through their pure parts and their failure
paths — the point of a test here is that a bad address, a dead service or a
non-Windows machine produces an honest refusal rather than a crash or a fake
success, which is what a live call could never prove on demand anyway.
"""

from __future__ import annotations

import pytest

from jarvis.capabilities import CapabilityRegistry, Risk
from jarvis.tools import load_tools
from jarvis.tools.find_capability import build as build_find
from jarvis.tools.get_time import _run as get_time
from jarvis.tools.open_app import best_match, list_shortcuts
from jarvis.webtext import to_text


@pytest.fixture
def reg():
    registry = CapabilityRegistry()
    load_tools(registry)
    return registry


def test_every_tool_loads_and_declares_a_risk(reg):
    specs = reg.list()
    assert len(specs) >= 8
    for spec in specs:
        assert isinstance(spec.risk, Risk), f"{spec.name} has no risk classification"
        assert spec.timeout_s > 0
        assert spec.input_schema.get("type") == "object"


def test_the_model_is_told_name_description_and_schema_only(reg):
    for declaration in reg.declarations():
        assert set(declaration) == {"name", "description", "parameters"}


def test_loading_twice_is_not_a_duplicate_error(reg):
    """Re-registering the same spec must be idempotent, or a reload at runtime
    would take the whole registry down."""
    before = len(reg.list())
    load_tools(reg)
    assert len(reg.list()) == before


# --- get_time ----------------------------------------------------------------

def test_the_time_carries_a_sentence_the_fast_path_can_say():
    result = get_time()
    assert result["speak"].startswith("It's ")
    assert "on" not in result["speak"], "no date unless asked"
    assert " on " in get_time(include_date=True)["speak"]


# --- open_app ----------------------------------------------------------------

def test_a_short_query_picks_the_closest_app_not_the_longest(tmp_path):
    shortcuts = [{"name": "Visual Studio Code", "path": "a"},
                 {"name": "Code Runner Companion Helper", "path": "b"}]
    assert best_match("code", shortcuts)["name"] == "Visual Studio Code"
    assert best_match("nothing like it", shortcuts) is None


def test_scanning_a_missing_start_menu_is_not_a_failure(tmp_path):
    assert list_shortcuts([tmp_path / "does-not-exist"]) == []


def test_open_app_refuses_honestly_off_windows(reg):
    result = reg.get("open_app").handler(name="notepad")
    # This container is not Windows, so the honest answer is a refusal.
    assert result["ok"] is False and "Windows" in result["error"]


# --- read_web_page -----------------------------------------------------------

def test_html_becomes_readable_text_without_a_parser_dependency():
    text = to_text("<html><head><style>p{color:red}</style></head>"
                   "<body><h1>Title</h1><p>First &amp; second.</p>"
                   "<script>alert(1)</script><p>Third.</p></body></html>")
    assert "alert" not in text and "color:red" not in text
    assert "First & second." in text and "Third." in text


def test_a_non_url_is_refused_before_any_request(reg):
    assert reg.get("read_web_page").handler(url="tell me about cats")["ok"] is False


# --- find_capability ---------------------------------------------------------

def test_finding_a_capability_unlocks_exactly_what_matched(reg):
    [spec] = build_find(reg)
    result = spec.handler(intent="what is the weather")
    assert result["unlock"] == ["get_weather"]


def test_a_request_matching_nothing_says_so_rather_than_offering_anything(reg):
    [spec] = build_find(reg)
    result = spec.handler(intent="fly me to the moon")
    assert result["found"] == [] and result["unlock"] == []


def test_matching_never_returns_the_whole_registry(reg):
    """Filler words appear in nearly every description; matching on them is the
    same as matching on nothing."""
    [spec] = build_find(reg)
    assert len(spec.handler(intent="what can you do for me please")["unlock"]) < len(reg.list())
