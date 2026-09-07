"""Connectors: the allowlist, the three mechanisms, and the two gates.

Most of these are about what is refused. A connector reaches outside this
machine, so the interesting behaviour is the path that does not resolve, the
tool the user turned off, and the risky name that still has to ask.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis.capabilities import CapabilityKind, CapabilityRegistry, Risk
from jarvis.connectors import api_client, capabilities, cli_client, files_connector, risk, store
from jarvis.connectors.files_connector import NotAllowed
from jarvis.db import reset_for_tests as reset_db


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


# --- how risky is a tool nobody here declared ---------------------------------

@pytest.mark.parametrize("name,description,expected", [
    ("get_weather", "Today's weather.", "safe"),
    ("list_files", "List a folder.", "safe"),
    ("read_page", "Read a page.", "safe"),
    ("send_email", "Send an email.", "risky"),
    ("delete-page", "Remove it.", "risky"),
    ("updatePet", "Change a pet's details.", "risky"),
    ("checkout", "Complete the purchase.", "risky"),
    ("write_file", "Write a file.", "risky"),
    ("move_file", "Move a file.", "risky"),
])
def test_classifying_a_connector_tool(name, description, expected):
    assert risk.classify(name, description) == expected


def test_a_proper_noun_is_not_split_into_a_scary_word():
    """"Search SharePoint" must not become "search share point". The original
    splits camelCase in descriptions too, which re-creates exactly the false
    positive that word-tokenising was introduced to remove."""
    assert risk.classify("sharepoint_search", "Search SharePoint for a document.") == "safe"


def test_a_word_inside_a_longer_word_does_not_count():
    assert risk.classify("notion-list-favorite-pages", "List pages in sidebar order.") == "safe"
    # "shared" is not the word "share", in either the name or the description.
    assert risk.classify("shared_link_reader", "Reads a link someone shared.") == "safe"


def test_only_the_first_sentence_of_a_long_description_is_read():
    """A real MCP tool's documentation runs to thousands of characters with
    worked examples; scanning all of it finds incidental words, not intent."""
    prose = ("Fetches a record. " + "It does not modify anything. " * 40
             + "Example: delete the cache first.")
    assert risk.classify("fetch_record", prose) == "safe"


def test_a_destructive_command_in_a_name_is_caught_as_a_fragment():
    assert risk.classify("run rm -rf /tmp", "") == "risky"


# --- the folder allowlist -----------------------------------------------------

def test_nothing_is_readable_until_a_folder_is_allowed(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(NotAllowed) as raised:
        files_connector.read_file(str(tmp_path / "notes.txt"), [])
    assert "ask the user for permission" in str(raised.value)


def test_a_path_outside_the_allowlist_is_refused(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (tmp_path / "secret.txt").write_text("no")
    with pytest.raises(NotAllowed):
        files_connector.read_file(str(tmp_path / "secret.txt"), [str(allowed)])


def test_a_relative_escape_is_resolved_before_it_is_checked(tmp_path):
    """A `..` segment looks like it stays where it is and does not."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (tmp_path / "secret.txt").write_text("no")
    with pytest.raises(NotAllowed):
        files_connector.read_file(str(allowed / ".." / "secret.txt"), [str(allowed)])


def test_a_neighbouring_folder_with_the_same_prefix_is_not_inside_it(tmp_path):
    allowed = tmp_path / "work"
    allowed.mkdir()
    sneaky = tmp_path / "work-private"
    sneaky.mkdir()
    (sneaky / "notes.txt").write_text("no")
    with pytest.raises(NotAllowed):
        files_connector.read_file(str(sneaky / "notes.txt"), [str(allowed)])


def test_reading_writing_listing_and_moving_inside_an_allowed_folder(tmp_path):
    # Its own folder: the scratch data directory shares this tmp_path.
    work = tmp_path / "work"
    work.mkdir()
    allowed = [str(work)]
    files_connector.write_file(str(work / "a.txt"), "hello", allowed)
    assert files_connector.read_file(str(work / "a.txt"), allowed)["content"] == "hello"

    listed = files_connector.list_files(str(work), allowed)
    assert [e["name"] for e in listed["entries"]] == ["a.txt"]

    files_connector.move_file(str(work / "a.txt"), str(work / "b.txt"), allowed)
    assert (work / "b.txt").exists() and not (work / "a.txt").exists()


def test_moving_a_file_out_of_the_allowlist_is_refused(tmp_path):
    """Both ends are checked: taking a file somewhere it may not go is still
    reaching somewhere it may not."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "a.txt").write_text("x")
    with pytest.raises(NotAllowed):
        files_connector.move_file(str(allowed / "a.txt"), str(tmp_path / "escaped.txt"),
                                  [str(allowed)])


def test_a_file_too_big_to_paste_is_refused_with_its_size(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * (files_connector.MAX_READ_BYTES + 10))
    with pytest.raises(ValueError) as raised:
        files_connector.read_file(str(big), [str(tmp_path)])
    assert "too big" in str(raised.value)


def test_allow_folder_records_a_real_folder_and_refuses_a_fake_one(tmp_path):
    from jarvis.tools.allow_folder import build

    registry = CapabilityRegistry()
    tool = build(registry)[0]

    refused = tool.handler(path=str(tmp_path / "nope"))
    assert refused["ok"] is False and "isn't a folder" in refused["error"]

    allowed = tool.handler(path=str(tmp_path))
    assert allowed["ok"] is True
    connector = store.get_or_create_singleton("files")
    assert connector["config"]["allowedFolders"] == [str(tmp_path.resolve())]

    # ...and the file tools appear only once there is somewhere to use them.
    assert registry.get("read_file") is not None


def test_allowing_a_folder_is_read_back_with_the_real_path(tmp_path):
    from jarvis.tools.allow_folder import build

    tool = build(CapabilityRegistry())[0]
    assert tool.risk is Risk.MEDIUM
    assert str(tmp_path) in tool.summarize({"path": str(tmp_path)})


# --- an HTTP API --------------------------------------------------------------

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "/api/v3"}],
    "paths": {
        "/pet/{petId}": {
            "get": {"operationId": "getPetById", "summary": "Find a pet by id.",
                    "parameters": [{"name": "petId", "required": True,
                                    "description": "The id."}]},
            "delete": {"operationId": "deletePet", "summary": "Delete a pet.",
                       "parameters": [{"name": "petId", "required": True}]},
        },
        "/pet": {"post": {"operationId": "addPet", "summary": "Add a pet.",
                          "requestBody": {"content": {}}}},
    },
}


class _Response:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload, self.status_code, self.text = payload, status_code, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_reading_an_openapi_document_into_operations():
    found = api_client.discover_from_spec("https://example.test/openapi.json",
                                          fetch=lambda url: _Response(SPEC))
    # A relative server URL is resolved against the document's own address.
    assert found["baseUrl"] == "https://example.test/api/v3"
    names = {op["name"]: op for op in found["operations"]}
    assert set(names) == {"getpetbyid", "deletepet", "addpet"}
    assert names["getpetbyid"]["parameters"]["required"] == ["petId"]
    assert "body" in names["addpet"]["parameters"]["properties"]


def test_the_older_swagger_shape_is_read_too():
    """Both shapes are still live, and a real example server uses the old one."""
    swagger = {"host": "example.test", "basePath": "/v2", "schemes": ["https"],
               "paths": {"/pet": {"get": {"operationId": "listPets", "summary": "List."}}}}
    found = api_client.discover_from_spec("https://example.test/swagger.json",
                                          fetch=lambda url: _Response(swagger))
    assert found["baseUrl"] == "https://example.test/v2"


def test_something_that_is_not_a_spec_says_so_plainly():
    with pytest.raises(ValueError) as raised:
        api_client.discover_from_spec("https://example.test/x",
                                      fetch=lambda url: _Response(payload=None, text="<html>"))
    assert "YAML spec isn't supported" in str(raised.value)


def test_a_document_with_no_endpoints_is_refused():
    with pytest.raises(ValueError):
        api_client.discover_from_spec("https://example.test/x",
                                      fetch=lambda url: _Response({"paths": {}}))


def test_calling_an_operation_fills_the_path_and_sends_the_key():
    from jarvis.config import save_secret

    save_secret("petstore", "sk-real-key")
    sent = {}

    def request(**kw):
        sent.update(kw)
        return _Response({"name": "Rex"}, text='{"name": "Rex"}')

    config = {"baseUrl": "https://example.test/api/v3", "secretRef": "petstore",
              "auth": {"kind": "bearer"},
              "operations": [{"name": "getpetbyid", "method": "GET", "path": "/pet/{petId}",
                              "parameters": {"type": "object", "properties": {}}}]}
    result = api_client.dispatch("getpetbyid", {"petId": "7", "verbose": "yes"}, config,
                                 request=request)

    assert result["ok"] is True
    assert sent["url"] == "https://example.test/api/v3/pet/7"
    assert sent["headers"]["Authorization"] == "Bearer sk-real-key"
    # A path placeholder is consumed, so it is not also sent as a query.
    assert sent["params"] == {"verbose": "yes"}


def test_an_api_error_is_reported_with_its_status_rather_than_raising():
    config = {"baseUrl": "https://example.test", "operations": [
        {"name": "getpet", "method": "GET", "path": "/pet"}]}
    result = api_client.dispatch("getpet", {}, config,
                                 request=lambda **kw: _Response(status_code=404,
                                                                text="no such pet"))
    assert result["ok"] is False and result["status"] == 404
    assert "no such pet" in result["error"]


# --- a command line program ---------------------------------------------------

CLI_CONFIG = {
    "command": "git",
    "commands": [
        {"name": "git_log", "description": "Recent commits.",
         "argv": ["log", "--oneline", "-n", "{count}"],
         "args": [{"name": "count", "description": "How many."}]},
    ],
}


def test_a_command_is_a_template_with_only_values_filled_in():
    """The model chooses values, never the command, never a flag. That is the
    whole reason this is allowed to exist at all."""
    ran = {}

    def run(**kw):
        ran.update(kw)
        return subprocess.CompletedProcess(args=kw["args"], returncode=0,
                                           stdout="abc123 fixed it\n", stderr="")

    result = cli_client.dispatch("git_log", {"count": "5"}, CLI_CONFIG, run=run)
    assert result["ok"] is True and "fixed it" in result["stdout"]
    assert ran["args"] == ["git", "log", "--oneline", "-n", "5"]


def test_a_value_cannot_smuggle_in_another_command():
    """No shell is involved, so a semicolon is just a character in an argument."""
    ran = {}

    def run(**kw):
        ran.update(kw)
        return subprocess.CompletedProcess(args=kw["args"], returncode=0, stdout="",
                                           stderr="")

    cli_client.dispatch("git_log", {"count": "5; rm -rf /"}, CLI_CONFIG, run=run)
    assert ran["args"][-1] == "5; rm -rf /"
    assert isinstance(ran["args"], list), "an argument list, never a shell string"


def test_a_missing_value_is_refused_rather_than_left_blank():
    with pytest.raises(ValueError) as raised:
        cli_client.dispatch("git_log", {}, CLI_CONFIG, run=lambda **kw: None)
    assert 'Missing required value "count"' in str(raised.value)


def test_a_failing_command_reports_its_real_error():
    def run(**kw):
        return subprocess.CompletedProcess(args=kw["args"], returncode=128, stdout="",
                                           stderr="not a git repository")

    result = cli_client.dispatch("git_log", {"count": "5"}, CLI_CONFIG, run=run)
    assert result["ok"] is False and "not a git repository" in result["error"]


def test_a_command_that_is_not_installed_says_so():
    config = {"command": "definitely-not-installed-anywhere", "commands": [
        {"name": "x", "argv": []}]}
    with pytest.raises(FileNotFoundError):
        cli_client.dispatch("x", {}, config)


# --- becoming something the model can call ------------------------------------

def _api_connector(**config):
    return store.add_connector(type="api", label="Pet Store", config={
        "baseUrl": "https://example.test", "operations": [
            {"name": "getpet", "description": "Find a pet.", "method": "GET",
             "path": "/pet"},
            {"name": "deletepet", "description": "Delete a pet.", "method": "DELETE",
             "path": "/pet"}],
        **config})


def test_a_connectors_tools_are_prefixed_with_its_own_label():
    """Two connectors can each offer a `search`."""
    _api_connector()
    registry = CapabilityRegistry()
    names = capabilities.sync(registry)
    assert names == ["pet_store__deletepet", "pet_store__getpet"]
    assert registry.get("pet_store__getpet").kind is CapabilityKind.CONNECTOR


def test_the_files_connector_keeps_its_plain_names():
    """There is only one of it, so nothing can collide."""
    store.get_or_create_singleton("files")
    registry = CapabilityRegistry()
    assert "read_file" in capabilities.sync(registry)


def test_a_risky_connector_tool_confirms_even_though_nobody_declared_it():
    _api_connector()
    registry = CapabilityRegistry()
    capabilities.sync(registry)
    assert registry.get("pet_store__getpet").risk is Risk.LOW
    assert registry.get("pet_store__deletepet").risk is Risk.MEDIUM


def test_a_tool_the_user_turned_off_is_absent_rather_than_refused():
    """Being told no is worse than never being offered: the model spends a step
    finding out, and then has to explain it."""
    connector = _api_connector()
    store.set_tool_permission(connector["id"], "deletepet", "deny")
    registry = CapabilityRegistry()
    assert capabilities.sync(registry) == ["pet_store__getpet"]


def test_marking_a_tool_ask_makes_it_confirm_without_changing_what_it_is():
    connector = _api_connector()
    store.set_tool_permission(connector["id"], "getpet", "ask")
    registry = CapabilityRegistry()
    capabilities.sync(registry)
    assert registry.get("pet_store__getpet").risk is Risk.MEDIUM


def test_always_allow_cannot_stop_a_risky_tool_asking():
    """Standing permission and runtime confirmation are separate, and neither
    can suppress the other."""
    connector = _api_connector()
    store.set_tool_permission(connector["id"], "deletepet", "allow")
    registry = CapabilityRegistry()
    capabilities.sync(registry)
    assert registry.get("pet_store__deletepet").risk is Risk.MEDIUM


def test_switching_a_connector_off_removes_its_tools():
    connector = _api_connector()
    registry = CapabilityRegistry()
    capabilities.sync(registry)
    store.update_connector(connector["id"], {"enabled": False})
    assert capabilities.sync(registry) == []
    assert registry.get("pet_store__getpet") is None


def test_a_removed_connector_stops_being_callable():
    connector = _api_connector()
    registry = CapabilityRegistry()
    capabilities.sync(registry)
    store.delete_connector(connector["id"])
    capabilities.sync(registry)
    assert registry.get("pet_store__getpet") is None


def test_a_call_reads_the_connector_fresh_rather_than_a_copy_from_setup(tmp_path):
    """An allowlist can change between a declaration being built and a tool
    being called, and acting on the old one is the mistake that only shows up
    once it matters."""
    connector = store.get_or_create_singleton("files")
    registry = CapabilityRegistry()
    capabilities.sync(registry)

    refused = registry.get("read_file").handler(path=str(tmp_path / "a.txt"))
    assert refused["ok"] is False

    (tmp_path / "a.txt").write_text("hello")
    store.update_connector(connector["id"],
                           {"config": {"allowedFolders": [str(tmp_path)]}})
    allowed = registry.get("read_file").handler(path=str(tmp_path / "a.txt"))
    assert allowed["ok"] is True and allowed["content"] == "hello"


def test_a_connector_that_fails_returns_a_result_rather_than_raising():
    _api_connector()
    registry = CapabilityRegistry()
    capabilities.sync(registry)

    def explode(**kw):
        raise RuntimeError("the service is down")

    import jarvis.connectors.api_client as module

    original = module.dispatch
    module.dispatch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the service is down"))
    try:
        result = registry.get("pet_store__getpet").handler()
    finally:
        module.dispatch = original
    assert result["ok"] is False and "the service is down" in result["error"]


# --- the routes ---------------------------------------------------------------

def test_a_key_never_leaves_the_server(live_server):
    """The config summary is built by naming what goes out, not by stripping
    what must not: a filter is a list somebody forgets to add to."""
    import httpx

    from jarvis.config import save_secret

    save_secret("petstore", "sk-real-key")
    _api_connector(secretRef="petstore")

    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        listed = client.get("/api/connectors").json()

    body = str(listed)
    assert "sk-real-key" not in body and "petstore" not in body
    api = next(c for c in listed["connectors"] if c["type"] == "api")
    assert api["config"] == {"hasSecret": True}


def test_connectors_can_be_listed_inspected_and_removed(live_server):
    import httpx

    connector = _api_connector()
    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        detail = client.get(f"/api/connectors/{connector['id']}").json()
        assert {t["name"] for t in detail["tools"]} == {"pet_store__getpet",
                                                        "pet_store__deletepet"}
        assert next(t for t in detail["tools"]
                    if t["name"] == "pet_store__deletepet")["confirms"] is True

        turned_off = client.patch(f"/api/connectors/{connector['id']}",
                                  json={"toolPermissions": {"deletepet": "deny"}}).json()
        assert turned_off["ok"] is True

        assert client.delete(f"/api/connectors/{connector['id']}").json()["ok"] is True
        assert client.get(f"/api/connectors/{connector['id']}").status_code == 404


# --- rendering a page that a plain fetch cannot read --------------------------

def test_a_page_that_needs_a_browser_is_rendered_invisibly(monkeypatch):
    from jarvis.tools.read_web_page import SPEC

    monkeypatch.setattr("jarvis.tools.read_web_page.get_text",
                        lambda url, **kw: "<html><body><div id='app'></div></body></html>")

    from jarvis.webrender import Rendered

    monkeypatch.setattr("jarvis.webrender.render",
                        lambda url, **kw: Rendered(ok=True, html="<html><body>"
                                                   + "The real article. " * 60
                                                   + "</body></html>"))
    result = SPEC.handler(url="https://example.test/app")
    assert result["ok"] is True and result["rendered"] is True
    assert "The real article." in result["text"]


def test_a_page_with_real_html_is_never_rendered(monkeypatch):
    from jarvis.tools.read_web_page import SPEC

    monkeypatch.setattr("jarvis.tools.read_web_page.get_text",
                        lambda url, **kw: "<html><body>" + "Plenty of real text. " * 60
                                          + "</body></html>")
    monkeypatch.setattr("jarvis.webrender.render",
                        lambda url, **kw: (_ for _ in ()).throw(
                            AssertionError("a readable page must not be rendered")))
    assert SPEC.handler(url="https://example.test/article")["rendered"] is False


def test_no_browser_available_is_reported_rather_than_looking_like_a_broken_page(monkeypatch):
    from jarvis.tools.read_web_page import SPEC

    monkeypatch.setattr("jarvis.tools.read_web_page.get_text", lambda url, **kw: "<html></html>")
    monkeypatch.setattr("jarvis.webrender.is_available", lambda: False)
    result = SPEC.handler(url="https://example.test/app")
    assert result["ok"] is False and "real browser" in result["error"]
