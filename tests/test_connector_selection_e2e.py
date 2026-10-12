"""Choosing between two connected apps that can both do the job, end to end.

Two real MCP servers that demand their own token, real discovery, the real registry, and the
real executor (argument validation, policy, dispatch) — not the search function on its own. What is
checked is the RESULT that came back from the app the person's words pointed at.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jarvis.capabilities import CapabilityRegistry, Risk
from jarvis.capabilities.execute import ExecOutcome, execute
from jarvis.config import save_secret
from jarvis.connectors import capabilities, store
from jarvis.db import reset_for_tests as reset_db
from jarvis.policy import Autonomy, CallContext, Surface
from jarvis.tools.find_capability import build as build_find

sys.path.insert(0, str(Path(__file__).parent))
from stub_mcp_server import StubMcpHttp  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


def _ctx(turn="t1") -> CallContext:
    return CallContext(session_id="s1", turn_id=turn, surface=Surface.TEXT, autonomy=Autonomy.INTERACTIVE)


def _app(label: str, stub: StubMcpHttp) -> dict:
    save_secret(f"conn_{label.lower()}", stub.token)
    connector = store.add_connector(type="mcp", label=label, config={
        "connectFlow": {"kind": "token", "url": stub.url}, "secretRef": f"conn_{label.lower()}"})
    store.update_connector(connector["id"], {"status": {"state": "working", "checkedAt": None,
                                                        "detail": None}})
    capabilities.refresh_tools(connector["id"])
    for tool in ("search_notes", "delete_note"):
        store.set_tool_permission(connector["id"], f"{label.lower()}__{tool}", "allow")
    return connector


@pytest.fixture
def two_apps():
    with StubMcpHttp(token="alpha-token") as alpha, StubMcpHttp(token="beta-token") as beta:
        _app("Alpha", alpha)
        _app("Beta", beta)
        registry = CapabilityRegistry()
        capabilities.sync(registry)
        for spec in build_find(registry):
            registry.register(spec)
        yield registry, alpha, beta


def _find(registry, **args):
    result = execute("find_capability", args, _ctx(), registry=registry)
    assert result.outcome is ExecOutcome.COMPLETED, result
    return result.value


def test_without_an_instruction_either_app_may_answer(two_apps):
    registry, *_ = two_apps
    assert {n.split("__")[0] for n in _find(registry, intent="search my notes")["unlock"]} == {"alpha", "beta"}


def test_told_not_to_use_one_the_other_does_the_job_and_its_result_comes_back(two_apps):
    registry, alpha, beta = two_apps
    found = _find(registry, intent="search my notes", avoid=["Alpha"])
    assert found["unlock"] and all(n.startswith("beta__") for n in found["unlock"])

    result = execute("beta__search_notes", {"query": "milk"}, _ctx("t2"), registry=registry)
    assert result.outcome is ExecOutcome.COMPLETED, result
    assert result.value["ok"] is True and "milk" in result.value["result"].lower()
    # the call went to Beta's server, with Beta's own token, and Alpha's was never asked
    assert beta.auth_seen and all(a == "Bearer beta-token" for a in beta.auth_seen)
    assert not any(c for c in alpha.calls)


def test_the_same_instruction_in_plain_words_does_the_same(two_apps):
    registry, *_ = two_apps
    found = _find(registry, intent="search my notes but don't use alpha")
    assert found["unlock"] and all(n.startswith("beta__") for n in found["unlock"])


def test_asked_for_by_name_that_app_comes_first_and_answers(two_apps):
    registry, alpha, beta = two_apps
    found = _find(registry, intent="search my notes", prefer="Alpha")
    assert found["unlock"][0].startswith("alpha__")
    result = execute(found["unlock"][0], {"query": "eggs"}, _ctx("t3"), registry=registry)
    assert result.value["ok"] is True
    assert alpha.auth_seen and all(a == "Bearer alpha-token" for a in alpha.auth_seen)
    assert not beta.calls


def test_leaving_out_the_only_app_that_can_says_so(two_apps):
    registry, *_ = two_apps
    answer = _find(registry, intent="search my notes", avoid=["Alpha", "Beta"])
    assert answer["found"] == [] and "left out" in answer["note"]


def test_a_wrongly_typed_instruction_is_refused_by_the_executor_not_silently_ignored(two_apps):
    registry, *_ = two_apps
    result = execute("find_capability", {"intent": "search my notes", "avoid": "Alpha"}, _ctx("t4"),
                     registry=registry)
    assert result.outcome is ExecOutcome.INVALID_ARGUMENTS
