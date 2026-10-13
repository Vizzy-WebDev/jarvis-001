"""A connected app's tool list keeps up with the app, with nobody clicking Refresh.

Seen live (Oct 2026): Higgsfield had 156 tools and Jarvis knew 101; Notion 49 against 45. The list is
read once at connect time and never again, so every tool an app adds afterwards was invisible to
Jarvis until someone opened the app's screen and pressed Refresh.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvis.config import save_secret
from jarvis.connectors import store, tool_refresh
from jarvis.db import reset_for_tests as reset_db

sys.path.insert(0, str(Path(__file__).parent))
from stub_mcp_server import StubMcpHttp  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    tool_refresh.stop()
    reset_db()


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat().replace("+00:00", "Z")


def _app(stub, label="Notes", *, refreshed: timedelta | None = timedelta(days=3), state="working",
         enabled=True, tools=0):
    save_secret(f"conn_{label.lower()}", stub.token)
    config = {"connectFlow": {"kind": "token", "url": stub.url}, "secretRef": f"conn_{label.lower()}",
              "tools": [{"name": f"old{i}", "description": "d", "parameters": {"type": "object"}}
                        for i in range(tools)]}
    if refreshed is not None:
        config["toolsRefreshedAt"] = _iso(refreshed)
    connector = store.add_connector(type="mcp", label=label, config=config)
    store.update_connector(connector["id"], {"enabled": enabled,
                                             "status": {"state": state, "checkedAt": None, "detail": None}})
    return connector["id"]


def _count(connector_id):
    return len((store.get_connector(connector_id)["config"].get("tools")) or [])


def test_an_outdated_list_is_refreshed_and_new_tools_appear():
    with StubMcpHttp(token="t") as stub:
        cid = _app(stub, tools=1)
        report = tool_refresh.refresh_stale()
        assert report["refreshed"] == [cid]
        assert _count(cid) == 2                       # the server's two tools replaced the stale one
        refreshed_at = store.get_connector(cid)["config"]["toolsRefreshedAt"]
        assert refreshed_at > _iso(timedelta(minutes=5))
        assert store.get_connector(cid)["status"]["state"] == "working"


def test_a_list_read_recently_is_left_alone():
    with StubMcpHttp(token="t") as stub:
        _app(stub, refreshed=timedelta(hours=2), tools=1)
        assert tool_refresh.refresh_stale()["refreshed"] == []
        assert not stub.auth_seen                      # the server was never contacted


def test_a_list_that_was_never_timestamped_counts_as_outdated():
    """Every connector connected before this existed has no timestamp."""
    with StubMcpHttp(token="t") as stub:
        cid = _app(stub, refreshed=None, tools=1)
        assert tool_refresh.refresh_stale()["refreshed"] == [cid]


@pytest.mark.parametrize("kwargs", [{"state": "error"}, {"state": "untested"}, {"enabled": False}])
def test_apps_that_are_broken_or_switched_off_are_not_touched(kwargs):
    with StubMcpHttp(token="t") as stub:
        _app(stub, **kwargs)
        assert tool_refresh.refresh_stale()["refreshed"] == []
        assert not stub.auth_seen


def test_one_unreachable_app_does_not_stop_the_others_or_downgrade_itself(monkeypatch):
    monkeypatch.setattr("jarvis.connectors.mcp_client.RETRY_PAUSE_S", 0.0)
    with StubMcpHttp(token="t") as good:
        dead = store.add_connector(type="mcp", label="Dead", config={
            "connectFlow": {"kind": "none", "url": "http://127.0.0.1:1/mcp"},
            "toolsRefreshedAt": _iso(timedelta(days=3))})
        store.update_connector(dead["id"], {"status": {"state": "working", "checkedAt": None,
                                                       "detail": "5 tools"}})
        ok = _app(good, "Good")
        report = tool_refresh.refresh_stale()
    assert report["refreshed"] == [ok] and [f["id"] for f in report["failed"]] == [dead["id"]]
    # a bad moment on the network is not a reason to call a working app broken
    assert store.get_connector(dead["id"])["status"]["state"] == "working"


def test_a_refused_sign_in_is_still_surfaced():
    with StubMcpHttp(token="right") as stub:
        save_secret("conn_x", "wrong")
        c = store.add_connector(type="mcp", label="X", config={
            "connectFlow": {"kind": "token", "url": stub.url}, "secretRef": "conn_x",
            "toolsRefreshedAt": _iso(timedelta(days=3))})
        store.update_connector(c["id"], {"status": {"state": "working", "checkedAt": None, "detail": None}})
        tool_refresh.refresh_stale()
    assert store.get_connector(c["id"])["status"]["state"] == "error"


def test_the_registry_learns_the_new_tools_too():
    from jarvis.assembly import get_registry

    with StubMcpHttp(token="t") as stub:
        _app(stub, "Fresh", tools=0)
        tool_refresh.refresh_stale()
    assert get_registry().get("fresh__search_notes") is not None


def test_it_only_runs_when_its_interlock_is_set(monkeypatch):
    monkeypatch.delenv(tool_refresh.ENABLE_ENV, raising=False)
    assert tool_refresh.start() is False
    monkeypatch.setenv(tool_refresh.ENABLE_ENV, "1")
    monkeypatch.setattr(tool_refresh, "FIRST_SWEEP_DELAY_S", 3600)
    assert tool_refresh.start() is True
    assert tool_refresh.start() is False               # already running
