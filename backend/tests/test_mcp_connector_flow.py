"""An MCP connector end to end, against a real MCP server that demands a token.

Everything here goes through the real client library, the real routes and the
real executor. Earlier tests checked that a token was LOOKED UP and that a
refresh function existed; both were true while no MCP tool could be listed or
called at all — the token never left the process, and the refresh route failed
on every MCP connector. These check what reached the server.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import pytest

from jarvis.capabilities import CapabilityRegistry, Risk
from jarvis.capabilities.execute import ExecOutcome, execute, execute_approved
from jarvis.config import save_secret
from jarvis.connectors import capabilities, mcp_client, store
from jarvis.db import reset_for_tests as reset_db
from jarvis.policy import Autonomy, CallContext, Surface

sys.path.insert(0, str(Path(__file__).parent))
from stub_mcp_server import TOOLS, StubMcpHttp  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


@pytest.fixture
def stub():
    with StubMcpHttp(token="tok-stub") as server:
        yield server


def _connector(stub, *, with_token: bool = True, tools: bool = False) -> dict:
    config = {"connectFlow": {"kind": "token", "url": stub.url}}
    if with_token:
        save_secret("conn_notes", stub.token)
        config["secretRef"] = "conn_notes"
    connector = store.add_connector(type="mcp", label="Notes", config=config)
    store.update_connector(connector["id"], {"status": {"state": "working", "checkedAt": None,
                                                        "detail": None}})
    if tools:
        capabilities.refresh_tools(connector["id"])
    return store.get_connector(connector["id"])


def _ctx(autonomy=Autonomy.INTERACTIVE, turn_id="t1") -> CallContext:
    return CallContext(session_id="s1", turn_id=turn_id, surface=Surface.TEXT, autonomy=autonomy)


def _registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    capabilities.sync(registry)
    return registry


# --- connecting and reading tools ---------------------------------------------

def test_the_token_actually_reaches_the_server(stub):
    connector = _connector(stub)
    tools = mcp_client.fetch_tools(connector["config"], connector_id=connector["id"])
    assert sorted(t["name"] for t in tools) == sorted(TOOLS)
    assert stub.auth_seen and all(a == "Bearer tok-stub" for a in stub.auth_seen)


def test_without_a_token_the_server_really_refuses(stub):
    """The stub is not a pushover — otherwise the test above proves nothing."""
    connector = _connector(stub, with_token=False)
    with pytest.raises(RuntimeError) as refused:
        mcp_client.fetch_tools(connector["config"], connector_id=connector["id"])
    assert stub.auth_seen and all(a is None for a in stub.auth_seen)
    # Said in words a person can act on — not "unhandled errors in a TaskGroup".
    assert str(refused.value) == ("The app didn't accept Jarvis's sign-in. "
                                  "Disconnect it and connect again.")


def test_an_unreachable_server_is_said_plainly():
    with pytest.raises(RuntimeError) as failed:
        mcp_client.fetch_tools({"connectFlow": {"url": "http://127.0.0.1:1/mcp"}})
    assert str(failed.value).startswith("Couldn't reach the app's server at http://127.0.0.1:1/mcp")


def test_refresh_over_http_reads_and_saves_the_tools(stub, live_server):
    connector = _connector(stub)
    with httpx.Client(base_url=live_server, timeout=30.0) as client:
        refreshed = client.post(f"/api/connectors/{connector['id']}/refresh")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["tools"] == 2
        detail = client.get(f"/api/connectors/{connector['id']}").json()
    # Every newly read tool asks first, and the screen is told so.
    assert {t["title"]: t["permission"] for t in detail["tools"]} == {
        "search_notes": "ask", "delete_note": "ask"}


def test_a_finished_sign_in_reads_the_tools_by_itself(stub, live_server, monkeypatch):
    connector = _connector(stub)

    async def signed_in(_query):
        return {"ok": True, "connectorId": connector["id"]}

    monkeypatch.setattr("jarvis.connectors.oauth.handle_callback", signed_in)
    with httpx.Client(base_url=live_server, timeout=30.0) as client:
        assert client.get("/api/connectors/oauth/callback?code=x&state=y").status_code == 200
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            tools = client.get(f"/api/connectors/{connector['id']}").json()["tools"]
            if tools:
                break
            time.sleep(0.2)
    assert sorted(t["title"] for t in tools) == sorted(TOOLS)


def test_a_failed_read_after_connecting_says_why_and_keeps_the_connection(stub, live_server,
                                                                        monkeypatch):
    connector = _connector(stub, with_token=False)

    async def signed_in(_query):
        return {"ok": True, "connectorId": connector["id"]}

    monkeypatch.setattr("jarvis.connectors.oauth.handle_callback", signed_in)
    with httpx.Client(base_url=live_server, timeout=30.0) as client:
        client.get("/api/connectors/oauth/callback?code=x&state=y")
        deadline = time.monotonic() + 20
        status = {}
        while time.monotonic() < deadline:
            status = client.get(f"/api/connectors/{connector['id']}").json()["connector"]["status"]
            if status.get("detail"):
                break
            time.sleep(0.2)
    assert status["state"] == "working"
    assert status["detail"] == ("Connected, but couldn't read its tools: The app didn't accept "
                                "Jarvis's sign-in. Disconnect it and connect again.")


# --- Allow / Ask / Deny, where the tool actually runs --------------------------

def test_a_new_tool_asks_and_nothing_reaches_the_server(stub):
    _connector(stub, tools=True)
    registry = _registry()
    assert registry.get("notes__search_notes").risk is Risk.HIGH

    result = execute("notes__search_notes", {"query": "plan"}, _ctx(), registry=registry)
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL
    assert stub.calls == []


@pytest.mark.parametrize("autonomy", [Autonomy.PRE_CONSENTED, Autonomy.ESCALATE])
def test_ask_is_not_waved_through_by_a_scheduled_task_or_a_background_job(stub, autonomy):
    _connector(stub, tools=True)
    result = execute("notes__search_notes", {"query": "plan"}, _ctx(autonomy), registry=_registry())
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL and result.approval_id
    assert stub.calls == []


def test_an_approved_ask_runs_once_approved_in_a_later_turn(stub):
    _connector(stub, tools=True)
    registry = _registry()
    asked = execute("notes__search_notes", {"query": "plan"}, _ctx(turn_id="t1"),
                    registry=registry)
    result = execute_approved(asked.approval_id, resolving_turn_id="t2",
                              ctx=_ctx(turn_id="t2"), registry=registry)
    assert result.ok, result.error
    assert "Weekly plan" in result.value["result"]
    assert stub.calls == [{"tool": "search_notes", "args": {"query": "plan"}}]


def test_allow_runs_the_real_tool_with_the_token(stub):
    connector = _connector(stub, tools=True)
    store.set_tool_permission(connector["id"], "notes__search_notes", "allow")
    registry = _registry()
    assert registry.get("notes__search_notes").risk is Risk.LOW

    result = execute("notes__search_notes", {"query": "plan"}, _ctx(Autonomy.PRE_CONSENTED),
                     registry=registry)
    assert result.ok, result.error
    assert result.value["ok"] is True and "Weekly plan" in result.value["result"]
    assert stub.calls == [{"tool": "search_notes", "args": {"query": "plan"}}]
    assert stub.auth_seen[-1] == "Bearer tok-stub"


@pytest.mark.parametrize("autonomy", [Autonomy.INTERACTIVE, Autonomy.PRE_CONSENTED,
                                      Autonomy.ESCALATE])
def test_always_allow_runs_without_asking_everywhere_even_a_risky_sounding_tool(stub, autonomy):
    """The person's setting is final: "delete" in the name no longer makes an
    Always-allowed tool ask in chat — nor anywhere else."""
    connector = _connector(stub, tools=True)
    store.set_tool_permission(connector["id"], "notes__delete_note", "allow")
    result = execute("notes__delete_note", {"note_id": "n1"}, _ctx(autonomy), registry=_registry())
    assert result.ok, result.error
    assert stub.calls == [{"tool": "delete_note", "args": {"note_id": "n1"}}]


def test_deny_is_not_offered_and_is_refused_even_if_called(stub):
    connector = _connector(stub, tools=True)
    registry = _registry()
    stale = registry.get("notes__search_notes")  # a declaration built before the change
    store.set_tool_permission(connector["id"], "notes__search_notes", "deny")

    assert not _registry().has("notes__search_notes")
    refused = stale.handler(query="plan")
    assert refused["ok"] is False and "turned" in refused["error"]
    assert stub.calls == []


def test_a_blocked_tool_stays_on_the_screen_and_can_be_unblocked(stub, live_server):
    connector = _connector(stub, tools=True)
    cid = connector["id"]
    with httpx.Client(base_url=live_server, timeout=30.0) as client:
        client.patch(f"/api/connectors/{cid}",
                     json={"toolPermissions": {"notes__search_notes": "deny",
                                               "notes__delete_note": "deny"}})
        blocked = client.get(f"/api/connectors/{cid}").json()["tools"]
        assert {t["permission"] for t in blocked} == {"deny"} and len(blocked) == 2

        client.patch(f"/api/connectors/{cid}", json={"toolPermissions": {"notes__search_notes": "allow"}})
        back = {t["name"]: t["permission"] for t in client.get(f"/api/connectors/{cid}").json()["tools"]}
    assert back == {"notes__search_notes": "allow", "notes__delete_note": "deny"}
    from jarvis.assembly import get_registry
    assert get_registry().get("notes__search_notes").risk is Risk.LOW
    assert not get_registry().has("notes__delete_note")


def test_the_builtin_files_and_browser_tools_are_not_turned_into_asks():
    files = store.get_or_create_singleton("files")
    assert store.tool_permission(files, "read_file") == "allow"


# --- stdio ---------------------------------------------------------------------

def test_a_local_stdio_server_lists_and_runs_tools():
    script = str(Path(__file__).parent / "stub_mcp_server.py")
    connector = store.add_connector(type="mcp", label="Local notes", config={
        "connectFlow": {"kind": "stdio", "command": sys.executable, "args": [script, "stdio"]}})
    capabilities.refresh_tools(connector["id"])
    store.set_tool_permission(connector["id"], "local_notes__search_notes", "allow")
    result = execute("local_notes__search_notes", {"query": "plan"}, _ctx(), registry=_registry())
    assert result.ok and "Weekly plan" in result.value["result"], result.error


# --- Jarvis knowing what is connected -----------------------------------------

def test_jarvis_is_told_what_is_set_up_in_connector(stub):
    """Asked "how many apps do we have connected", Jarvis once had nothing to go
    on and answered from one app's own "list connectors" tool. The Connector
    records themselves are now in its instruction, every turn, as they stand."""
    from jarvis.prompt import connected_apps_section, system_instruction

    assert connected_apps_section().endswith("- None yet.")

    connector = _connector(stub, tools=True)
    store.set_tool_permission(connector["id"], "notes__delete_note", "deny")
    store.add_connector(type="mcp", label="Mail", config={"connectFlow": {"url": stub.url}})
    off = store.add_connector(type="api", label="Weather", config={"baseUrl": "https://w.invalid"})
    store.update_connector(off["id"], {"enabled": False})

    section = connected_apps_section()
    assert "- Notes — connected; 2 tools, 1 of them usable and 1 blocked by the user (tool names start with notes__)" in section
    assert "- Mail — added, but not connected yet." in section
    assert "1 of 3 connected." in section
    assert "- Weather — switched off by the user" in section
    assert "Files" not in section and "Browser" not in section  # Jarvis's own, not apps
    assert section in system_instruction()


def test_a_connector_being_saved_is_never_read_as_removed():
    """On Windows a read that lands while the file is being swapped in raised
    PermissionError, which was read as "no connectors": a live Composio call
    told the user "That connection has been removed" while they reconnected it
    (15% of reads during repeated saves). A read now waits for the swap."""
    import threading

    connector = store.add_connector(type="mcp", label="Busy", config={
        "connectFlow": {"url": "http://127.0.0.1:1/mcp"},
        "tools": [{"name": f"t{i}", "description": "d" * 400} for i in range(40)]})
    stop = threading.Event()

    def keep_saving():
        while not stop.is_set():
            store.update_connector(connector["id"], {"status": {
                "state": "working", "checkedAt": None, "detail": str(time.time())}})

    writer = threading.Thread(target=keep_saving)
    writer.start()
    try:
        missing = sum(store.get_connector(connector["id"]) is None for _ in range(600))
    finally:
        stop.set()
        writer.join()
    assert missing == 0
