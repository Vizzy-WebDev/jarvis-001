"""Asking for an app BY NAME must be answered about that app.

Found while investigating "Jarvis used Composio / Higgsfield when I told it to use
GitHub": nothing is hard-coded to a service, but `find_capability` only ever scored tool
names and descriptions. With GitHub in the Official directory yet never connected, the
one thing mentioning GitHub was an aggregator whose description lists 500 apps — so
that is what came back, with no word that the app the person named was not set up. The
model then used the aggregator and, correctly by its own information, saw no conflict.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jarvis.capabilities import CapabilityRegistry, CapabilitySpec, Risk
from jarvis.connectors import store
from jarvis.db import reset_for_tests as reset_db
from jarvis.tools.find_capability import build as build_find

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


def _aggregator_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        id="connector.agg.search", name="aggregator__search_tools",
        description="Connects 500+ apps: Slack, GitHub, Notion, Google Workspace. Search tools.",
        input_schema={"type": "object", "properties": {}}, risk=Risk.LOW, handler=lambda: {}))
    return registry


def _find(registry, intent):
    [spec] = build_find(registry)
    return spec.handler(intent=intent)


def test_a_named_app_in_the_directory_that_is_not_set_up_is_said_plainly():
    answer = _find(_aggregator_registry(), "create a github issue")
    assert answer["notConnected"] == [{"app": "GitHub", "state": "not set up yet"}]
    assert "GitHub" in answer["note"] and "different app" in answer["note"]


def test_a_saved_app_that_needs_reconnecting_says_why():
    store.add_connector(type="mcp", label="Canva", config={})
    [canva] = [c for c in store.list_connectors() if c["label"] == "Canva"]
    store.update_connector(canva["id"], {"status": {"state": "error", "checkedAt": None,
                                                    "detail": "The app didn't accept Jarvis's sign-in."}})
    answer = _find(CapabilityRegistry(), "make a canva design")
    assert answer["notConnected"][0]["app"] == "Canva"
    assert "needs reconnecting" in answer["notConnected"][0]["state"]
    assert "sign-in" in answer["notConnected"][0]["state"]


def test_an_app_that_is_connected_with_tools_raises_no_warning():
    store.add_connector(type="mcp", label="Notion", config={"tools": [
        {"name": "search", "description": "Search Notion", "parameters": {"type": "object"}}]})
    [notion] = [c for c in store.list_connectors() if c["label"] == "Notion"]
    store.update_connector(notion["id"], {"status": {"state": "working", "checkedAt": None,
                                                      "detail": "1 tools"}})
    assert "notConnected" not in _find(CapabilityRegistry(), "search notion")


def test_no_named_app_means_no_extra_note():
    assert "notConnected" not in _find(_aggregator_registry(), "what is the weather")
