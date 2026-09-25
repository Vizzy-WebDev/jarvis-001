"""API-key and CLI connectors, end to end against a real HTTP API and a real program.

Connection -> tools -> permission -> execution, through the real routes, the real
registry and the real executor. The stubs behave the ways real services were
found to behave (a key checked by a 404/401 endpoint; a verdict in the body of
an HTTP 200; a background job that is followed until done; a CLI that must sign
in first and is, on Windows, a `.cmd` script).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import pytest

from jarvis.capabilities import CapabilityRegistry, Risk
from jarvis.capabilities.execute import ExecOutcome, execute
from jarvis.connectors import api_client, capabilities, catalog, cli_client, store
from jarvis.db import reset_for_tests as reset_db
from jarvis.policy import Autonomy, CallContext, Surface

sys.path.insert(0, str(Path(__file__).parent))
from stub_api_server import GOOD_KEY, StubApi  # noqa: E402

IS_WINDOWS = os.name == "nt"


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


@pytest.fixture
def api():
    with StubApi() as server:
        yield server


@pytest.fixture
def stubcli(tmp_path, monkeypatch):
    """`stubcli` on the PATH — a `.cmd` script on Windows, like an npm-installed CLI."""
    script = Path(__file__).with_name("stub_cli.py")
    folder = tmp_path / "bin"
    folder.mkdir()
    if IS_WINDOWS:
        (folder / "stubcli.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n',
                                            encoding="utf-8")
    else:
        launcher = folder / "stubcli"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                            encoding="utf-8")
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("STUB_CLI_HOME", str(tmp_path))
    return folder


def _ctx(autonomy=Autonomy.INTERACTIVE, turn="t1") -> CallContext:
    return CallContext(session_id="s1", turn_id=turn, surface=Surface.TEXT, autonomy=autonomy)


def _registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    capabilities.sync(registry)
    return registry


def _api_config(api, **extra) -> dict:
    return {"baseUrl": api.url, "auth": {"kind": "bearer", "prefix": "Key"},
            "test": {"method": "GET", "path": "/requests/0/status", "okStatus": [200, 404]},
            "operations": [
                {"name": "create_job", "method": "POST", "path": "/jobs/create",
                 "description": "Start a job.",
                 "parameters": {"type": "object",
                                "properties": {"body": {"type": "object"}}},
                 "wait": {"idFrom": "data.taskId", "path": "/jobs/status",
                          "query": {"taskId": "{id}"}, "statusField": "data.state",
                          "done": ["success"], "failed": ["fail"], "intervalS": 1,
                          "timeoutS": 30}},
                {"name": "credit", "method": "GET", "path": "/credit",
                 "description": "Remaining credits."}],
            **extra}


def _wait_for_status(client, cid, want, seconds=20):
    deadline = time.monotonic() + seconds
    status = {}
    while time.monotonic() < deadline:
        status = client.get(f"/api/connectors/{cid}").json()["connector"]["status"]
        if status.get("state") == want:
            return status
        time.sleep(0.2)
    return status


# --- the Official directory ---------------------------------------------------

def test_the_directory_lists_api_and_cli_entries_as_their_own_types(live_server):
    with httpx.Client(base_url=live_server, timeout=30) as client:
        listed = client.get("/api/connectors/catalog").json()["catalog"]
        kinds = {e["id"]: e.get("type", "mcp") for e in listed}
        assert kinds["higgsfield-api"] == "api" and kinds["higgsfield-cli"] == "cli"
        assert kinds["notion"] == "mcp" and "type" not in next(
            e for e in listed if e["id"] == "notion")

        made = client.post("/api/connectors/catalog/higgsfield-api/ensure").json()
        detail = client.get(f"/api/connectors/{made['connectorId']}").json()
    assert detail["connector"]["type"] == "api"
    assert detail["setup"]["baseUrl"] == "https://api.higgsfield.ai"
    assert detail["setup"]["hasSecret"] is False
    # Every tool from the definition, each starting on Ask — the same as MCP.
    assert {t["title"]: t["permission"] for t in detail["tools"]} == {
        "generate": "ask", "get_request_status": "ask", "cancel_request": "ask"}


def test_every_official_entry_is_a_valid_definition():
    for entry in catalog.list_catalog():
        config = catalog.config_for(entry)
        if catalog.entry_type(entry) == "api":
            assert api_client.validate_operations(config["operations"])
        elif catalog.entry_type(entry) == "cli":
            assert cli_client.validate_commands(config["commands"])


# --- API key: connection --------------------------------------------------------

def test_a_wrong_key_is_refused_plainly_and_a_right_one_connects(api, live_server):
    connector = store.add_connector(type="api", label="Stub Media", config=_api_config(api))
    cid = connector["id"]
    with httpx.Client(base_url=live_server, timeout=30) as client:
        wrong = client.post(f"/api/connectors/{cid}/key", json={"apiKey": "nope"}).json()
        assert wrong["connected"] is False
        assert "refused the key (401)" in wrong["detail"]
        assert client.get(f"/api/connectors/{cid}").json()["connector"]["status"]["state"] \
            == "error"

        right = client.post(f"/api/connectors/{cid}/key", json={"apiKey": GOOD_KEY}).json()
        assert right["connected"] is True
        detail = client.get(f"/api/connectors/{cid}").json()
    assert detail["connector"]["status"]["state"] == "working"
    assert detail["setup"]["hasSecret"] is True
    assert GOOD_KEY not in str(detail), "a saved key is never returned"
    assert api.calls[-1]["auth"] == f"Key {GOOD_KEY}"


def test_a_service_that_answers_200_with_an_error_in_the_body_is_not_success(api):
    """Found live: Kie.ai answers HTTP 200 with {"code": 401, ...} for a bad key."""
    from jarvis.config import save_secret

    save_secret("conn_body", "wrong-key")
    config = _api_config(api, secretRef="conn_body",
                         test={"method": "GET", "path": "/credit"},
                         responseCheck={"field": "code", "ok": [200], "message": "msg"})
    result = api_client.check(config)
    assert result["ok"] is False and "Unauthorized - check your key" in result["detail"]
    called = api_client.dispatch("credit", {}, config)
    assert called["ok"] is False and "Unauthorized" in called["error"]

    save_secret("conn_body", GOOD_KEY)
    assert api_client.check(config)["ok"] is True
    assert api_client.dispatch("credit", {}, config)["ok"] is True


def test_with_no_test_set_it_says_it_was_not_checked(api):
    config = _api_config(api)
    config.pop("test")
    result = api_client.check(config)
    assert result["ok"] is True and "wasn't checked" in result["detail"]


# --- API key: long-running jobs ------------------------------------------------------

def test_a_background_job_is_followed_until_it_finishes(api):
    from jarvis.config import save_secret

    save_secret("conn_jobs", GOOD_KEY)
    config = _api_config(api, secretRef="conn_jobs")
    result = api_client.dispatch("create_job", {"body": {"model": "fine"}}, config)
    assert result["ok"] is True and result["jobStatus"] == "success"
    assert "result.png" in result["body"]
    assert [c["path"] for c in api.calls].count("/jobs/status") == 3


def test_a_failed_job_is_reported_as_failed_not_done(api):
    from jarvis.config import save_secret

    save_secret("conn_jobs", GOOD_KEY)
    result = api_client.dispatch("create_job", {"body": {"model": "broken"}},
                                 _api_config(api, secretRef="conn_jobs"))
    assert result["ok"] is False and result["jobStatus"] == "fail"


def test_a_job_still_running_at_the_limit_hands_back_its_id_without_claiming_done(api):
    from jarvis.config import save_secret

    save_secret("conn_jobs", GOOD_KEY)
    ticks = iter(range(0, 10_000, 20))
    result = api_client.dispatch("create_job", {"body": {"model": "slow"}},
                                 _api_config(api, secretRef="conn_jobs"),
                                 sleep=lambda s: None, clock=lambda: float(next(ticks)))
    assert result["ok"] is True and result["pending"] is True
    assert result["jobId"] == "t1" and "has not finished" in result["note"]


# --- API key: permission and execution ------------------------------------------------

def test_api_tools_ask_first_then_run_on_allow_and_vanish_on_block(api):
    from jarvis.config import save_secret

    save_secret("conn_perm", GOOD_KEY)
    connector = store.add_connector(type="api", label="Stub Media",
                                    config=_api_config(api, secretRef="conn_perm"))
    registry = _registry()
    assert registry.get("stub_media__credit").risk is Risk.HIGH
    asked = execute("stub_media__credit", {}, _ctx(), registry=registry)
    assert asked.outcome is ExecOutcome.NEEDS_APPROVAL
    assert not any(c["path"] == "/credit" for c in api.calls)

    store.set_tool_permission(connector["id"], "stub_media__credit", "allow")
    ran = execute("stub_media__credit", {}, _ctx(Autonomy.PRE_CONSENTED), registry=_registry())
    assert ran.ok and '"data": 42' in ran.value["body"]
    assert api.calls[-1]["auth"] == f"Key {GOOD_KEY}"

    store.set_tool_permission(connector["id"], "stub_media__credit", "deny")
    assert not _registry().has("stub_media__credit")


def test_a_waiting_tool_gets_a_time_limit_long_enough_to_finish(api):
    store.add_connector(type="api", label="Stub Media", config=_api_config(api))
    registry = _registry()
    assert registry.get("stub_media__create_job").timeout_s >= 90
    assert registry.get("stub_media__credit").timeout_s == 60


# --- API key: custom connectors and where their tools come from --------------------------

def test_openapi_import_proposes_endpoints_and_only_the_chosen_ones_are_saved(api, live_server):
    with httpx.Client(base_url=live_server, timeout=30) as client:
        made = client.post("/api/connectors", json={
            "type": "api", "label": "Pets", "config": {"baseUrl": api.url + "/v1"}}).json()
        cid = made["connector"]["id"]
        proposed = client.post(f"/api/connectors/{cid}/import-openapi",
                               json={"specUrl": api.url + "/openapi.yaml"}).json()
        assert proposed["ok"] is True, proposed
        assert proposed["auth"] == {"kind": "header", "name": "X-Pet-Key"}
        by_name = {o["name"]: o for o in proposed["operations"]}
        assert by_name["getpet"]["parameters"]["properties"]["petId"]["type"] == "integer"
        body = by_name["addpet"]["parameters"]["properties"]["body"]
        assert body["properties"]["name"]["type"] == "string", "the real body fields"
        assert body["properties"]["tags"]["items"] == {"type": "string"}
        assert client.get(f"/api/connectors/{cid}").json()["tools"] == [], "nothing saved yet"

        saved = client.patch(f"/api/connectors/{cid}", json={"config": {
            "operations": [by_name["getpet"]], "auth": proposed["auth"]}})
        assert saved.status_code == 200
        tools = client.get(f"/api/connectors/{cid}").json()["tools"]
    assert [t["title"] for t in tools] == ["getpet"]


def test_a_hand_added_endpoint_gets_its_path_parameters_by_itself():
    ops = api_client.validate_operations([{"name": "Get Item", "method": "get",
                                           "path": "/items/{itemId}"}])
    assert ops[0]["name"] == "get_item" and ops[0]["method"] == "GET"
    assert ops[0]["parameters"]["required"] == ["itemId"]
    with pytest.raises(ValueError):
        api_client.validate_operations([{"name": "x", "path": "no-slash"}])
    with pytest.raises(ValueError):
        api_client.validate_operations([{"name": "x", "path": "/x", "wait": {"idFrom": "id"}}])


def test_a_request_cannot_point_a_connector_at_someone_elses_key(api, live_server):
    """`secretRef` names which saved secret is SENT. From a request body it could
    aim an API connector at a model provider's key and ship it to any address."""
    from jarvis.config import save_secret

    save_secret("model_prov_x", "sk-provider-secret")
    with httpx.Client(base_url=live_server, timeout=30) as client:
        made = client.post("/api/connectors", json={
            "type": "api", "label": "Sneaky",
            "config": {"baseUrl": api.url, "secretRef": "model_prov_x"}}).json()
        cid = made["connector"]["id"]
        client.patch(f"/api/connectors/{cid}", json={"config": {"secretRef": "model_prov_x",
                                                                "env": {"X": "model_prov_x"}}})
    config = store.get_connector(cid)["config"]
    assert "secretRef" not in config and "env" not in config


# --- CLI: connection --------------------------------------------------------------

def _cli_config(**extra) -> dict:
    return {"command": "stubcli", "install": "pip install stubcli",
            "login": ["login"], "test": ["status", "--json"],
            "commands": [{"name": "greet", "description": "Greet someone.",
                          "argv": ["greet", "{name}"],
                          "args": [{"name": "name", "description": "Who."}]},
                         {"name": "echo_env", "argv": ["echo-env"], "args": []}],
            **extra}


def test_a_cli_that_is_not_installed_says_how_to_install_it(monkeypatch):
    result = cli_client.check({"command": "definitely-not-here-xyz",
                               "install": "npm i -g not-here"})
    assert result["ok"] is False and result["installed"] is False
    assert "npm i -g not-here" in result["detail"]


def test_signing_a_cli_in_turns_it_from_not_connected_to_working(stubcli, live_server):
    connector = store.add_connector(type="cli", label="Stub CLI", config=_cli_config())
    cid = connector["id"]
    with httpx.Client(base_url=live_server, timeout=30) as client:
        first = client.post(f"/api/connectors/{cid}/test").json()
        assert first["connected"] is False
        assert "not signed in" in first["detail"] and "Use Sign in" in first["detail"]

        assert client.post(f"/api/connectors/{cid}/login").json()["ok"] is True
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            again = client.post(f"/api/connectors/{cid}/test").json()
            if again["connected"]:
                break
            time.sleep(0.3)
        detail = client.get(f"/api/connectors/{cid}").json()
    assert again["connected"] is True
    assert detail["connector"]["status"]["state"] == "working"
    assert detail["setup"]["canSignIn"] is True


# --- CLI: permission and execution ----------------------------------------------------

def test_cli_tools_ask_first_then_run_on_allow(stubcli):
    connector = store.add_connector(type="cli", label="Stub CLI", config=_cli_config())
    registry = _registry()
    assert registry.get("stub_cli__greet").risk is Risk.HIGH
    asked = execute("stub_cli__greet", {"name": "Ada"}, _ctx(), registry=registry)
    assert asked.outcome is ExecOutcome.NEEDS_APPROVAL

    store.set_tool_permission(connector["id"], "stub_cli__greet", "allow")
    ran = execute("stub_cli__greet", {"name": "Ada"}, _ctx(), registry=_registry())
    assert ran.ok, ran.error
    assert "Hello, Ada!" in ran.value["stdout"]


@pytest.mark.skipif(not IS_WINDOWS, reason="the .cmd launch problem is Windows-only")
def test_a_cmd_script_cli_runs_and_refuses_values_the_shell_would_obey(stubcli):
    """npm-installed CLIs are .cmd scripts on Windows; run by bare name they failed
    with "[WinError 2]". Started through cmd.exe, `&` in a value would run a
    second command — so such values are refused for a script target."""
    assert (stubcli / "stubcli.cmd").exists()
    config = _cli_config()
    ok = cli_client.dispatch("greet", {"name": "Ada"}, config)
    assert ok["ok"] is True and "Hello, Ada!" in ok["stdout"]
    with pytest.raises(ValueError) as refused:
        cli_client.dispatch("greet", {"name": "Ada & calc"}, config)
    assert "command interpreter" in str(refused.value)


def test_a_cli_gets_only_its_own_key(stubcli, live_server, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-must-not-leak")
    connector = store.add_connector(type="cli", label="Stub CLI", config=_cli_config())
    cid = connector["id"]
    with httpx.Client(base_url=live_server, timeout=30) as client:
        saved = client.post(f"/api/connectors/{cid}/env-secret",
                            json={"name": "STUBCLI_TOKEN", "value": "tok-123"}).json()
        assert saved["ok"] is True
        detail = client.get(f"/api/connectors/{cid}").json()
    assert detail["setup"]["envNames"] == ["STUBCLI_TOKEN"]
    assert "tok-123" not in str(detail)
    ran = cli_client.dispatch("echo_env", {}, store.get_connector(cid)["config"])
    assert '"hasToken": true' in ran["stdout"]
    assert '"sawGeminiKey": false' in ran["stdout"]


# --- CLI: where its commands come from -------------------------------------------------

def test_help_discovery_proposes_only_commands_the_help_really_lists(stubcli, live_server,
                                                                     monkeypatch):
    from jarvis.ai import Answer

    def fake_ask(prompt, **kw):
        assert "stubcli - a tiny test program" in prompt, "the REAL help text was read"
        return Answer(data={"commands": [
            {"name": "greet", "description": "Greet.", "argv": ["greet", "{name}"],
             "args": [{"name": "name", "description": "Who."}]},
            {"name": "invented", "description": "Not real.", "argv": ["teleport"]},
            {"name": "sneaky", "description": "Two words.", "argv": ["greet", "a b"]}]})

    monkeypatch.setattr("jarvis.ai.ask", fake_ask)
    connector = store.add_connector(type="cli", label="Stub CLI",
                                    config={"command": "stubcli", "commands": []})
    with httpx.Client(base_url=live_server, timeout=60) as client:
        found = client.post(f"/api/connectors/{connector['id']}/discover-commands").json()
    assert found["ok"] is True, found
    assert [c["name"] for c in found["proposed"]] == ["greet"]
    assert store.get_connector(connector["id"])["config"]["commands"] == [], "nothing saved"


def test_a_hand_added_command_must_be_one_word_per_part():
    ok = cli_client.validate_commands([{"name": "Say Hi", "argv": ["greet", "{who}"]}])
    assert ok[0]["name"] == "say_hi" and ok[0]["args"][0]["name"] == "who"
    with pytest.raises(ValueError):
        cli_client.validate_commands([{"name": "bad", "argv": ["greet --force"]}])


def test_nothing_to_check_is_not_shown_as_connected_and_jarvis_is_told_honestly(api, live_server):
    """Found in the user's own first try: an API connector with no test and a
    wrong address showed Connected, because "nothing to check" was recorded as
    working. Now it stays unchecked, and a failed one tells Jarvis why."""
    from jarvis.prompt import connected_apps_section

    config = _api_config(api)
    config.pop("test")
    unchecked = store.add_connector(type="api", label="Unchecked API", config=config)
    failing = store.add_connector(type="api", label="Failing API", config=_api_config(api))
    with httpx.Client(base_url=live_server, timeout=30) as client:
        first = client.post(f"/api/connectors/{unchecked['id']}/test").json()
        client.post(f"/api/connectors/{failing['id']}/key", json={"apiKey": "wrong"})
        state = client.get(f"/api/connectors/{unchecked['id']}").json()["connector"]["status"]
    assert first["connected"] is True and state["state"] == "untested"
    section = connected_apps_section()
    assert "- Unchecked API — set up, connection not checked; 2 tools" in section
    assert "- Failing API — not connected: The service refused the key (401)" in section


def test_a_proposal_wrapped_in_a_code_fence_is_still_read(stubcli):
    """Found live with `gh`: the model put its JSON inside a ```json fence and
    every proposal was lost to a strict json.loads."""
    from jarvis.ai import Answer

    fenced = ('Here you go:\n```json\n{"commands": [{"name": "greet", "argv": ["greet", "{name}"],'
              ' "args": [{"name": "name", "description": "Who."}]}]}\n```\nHope that helps.')
    found = cli_client.discover_commands({"command": "stubcli"},
                                         ask=lambda prompt, **kw: Answer(text=fenced))
    assert [c["name"] for c in found["proposed"]] == ["greet"]
