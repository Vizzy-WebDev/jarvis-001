"""Setting up API-key and CLI connectors through the real Connector pop-up.

A real browser, the real built front end, the real app on a scratch port, and
real stand-ins on the other end: an HTTP API that checks its key, and a program
on the PATH that has to be signed in (a `.cmd` script on Windows, like an
npm-installed CLI). What these catch is a screen that renders but never reaches
its route, or reaches it and shows something other than what happened.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from stub_api_server import GOOD_KEY, StubApi  # noqa: E402
from test_shell_e2e import CHROME, EXPORT, page, visit  # noqa: E402,F401

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

pytestmark = [
    pytest.mark.skipif(not EXPORT.exists(), reason="the front end has not been built"),
    pytest.mark.skipif(CHROME is None, reason="no Chromium in this environment"),
]


def _open_connectors(page):
    page.evaluate("() => { window.location.hash = '#/app-control'; }")
    page.wait_for_url("**#/app-control")


def _add_custom(page, mechanism: str, label: str) -> None:
    page.click("[data-testid=add-connector-menu]")
    page.click("[data-testid=add-custom-connector]")
    page.click(f"[data-testid=mechanism-{mechanism}]")
    page.fill("[data-testid=custom-label]", label)


def test_the_directory_marks_api_and_cli_entries(page):
    _open_connectors(page)
    page.click("[data-testid=add-connector-menu]")
    page.click("[data-testid=browse-connectors]")
    page.wait_for_selector("[data-testid=catalog-row]")
    api_row = page.locator("[data-testid=catalog-row]", has_text="Higgsfield API")
    cli_row = page.locator("[data-testid=catalog-row]", has_text="Higgsfield CLI")
    assert api_row.locator("[data-testid=catalog-type]").inner_text() == "API"
    assert cli_row.locator("[data-testid=catalog-type]").inner_text() == "CLI"

    # Opening an API entry sets it up from its definition: key form, tools on Ask.
    api_row.click()
    page.wait_for_selector("[data-testid=api-connection]")
    page.wait_for_selector("[data-testid=tool-row][data-tool=generate][data-permission=ask]")
    assert page.locator("[data-testid=api-key-input]").count() == 1


def test_a_custom_api_connector_from_key_to_tools(page):
    """Wrong key -> refused plainly; right key -> Connected; an endpoint added by
    hand and others imported from the service's own OpenAPI document appear as
    tools on Need approval."""
    with StubApi() as api:
        _open_connectors(page)
        _add_custom(page, "api", "Stub Pets")
        page.fill("[data-testid=custom-base-url]", api.url)
        page.click("[data-testid=api-advanced-toggle]")
        page.fill("[data-testid=custom-auth-prefix]", "Key")
        page.fill("[data-testid=custom-test-path]", "/requests/0/status")
        page.fill("[data-testid=custom-test-ok]", "200, 404")
        page.click("[data-testid=save-custom-connector]")
        page.wait_for_selector("[data-testid=api-connection]")

        page.fill("[data-testid=api-key-input]", "not-the-key")
        page.click("[data-testid=save-api-key]")
        page.wait_for_selector("[data-testid=connection-message]:has-text('refused the key (401)')")
        page.wait_for_selector("[data-testid=connection-status][data-state=error]")

        page.fill("[data-testid=api-key-input]", GOOD_KEY)
        page.click("[data-testid=save-api-key]")
        page.wait_for_selector("[data-testid=connection-status][data-state=working]")
        assert api.calls[-1]["auth"] == f"Key {GOOD_KEY}"

        page.click("[data-testid=add-endpoint-toggle]")
        page.fill("[data-testid=endpoint-name]", "credit")
        page.fill("[data-testid=endpoint-path]", "/credit")
        page.fill("[data-testid=endpoint-description]", "Remaining credits.")
        page.click("[data-testid=save-endpoint]")
        page.wait_for_selector("[data-testid=tool-row][data-tool=credit][data-permission=ask]")

        page.click("[data-testid=import-openapi-toggle]")
        page.fill("[data-testid=spec-url]", api.url + "/openapi.yaml")
        page.click("[data-testid=read-spec]")
        page.wait_for_selector("[data-testid=proposed-endpoint][data-name=getpet]")
        page.uncheck("[data-testid=proposed-endpoint][data-name=addpet]")
        page.click("[data-testid=add-chosen-endpoints]")
        page.wait_for_selector("[data-testid=tool-row][data-tool=getpet][data-permission=ask]")
        assert page.locator("[data-testid=tool-row][data-tool=addpet]").count() == 0

        # A tool can be taken away again.
        page.click("[data-testid=tool-row][data-tool=credit] [data-testid=remove-tool]")
        page.wait_for_selector("[data-testid=tool-row][data-tool=credit]", state="detached")


def test_a_custom_cli_connector_signs_in_and_gets_a_command(page, tmp_path, monkeypatch):
    script = Path(__file__).with_name("stub_cli.py")
    folder = tmp_path / "bin"
    folder.mkdir()
    if os.name == "nt":
        (folder / "stubcli.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n',
                                            encoding="utf-8")
    else:
        launcher = folder / "stubcli"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                            encoding="utf-8")
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("STUB_CLI_HOME", str(tmp_path))

    _open_connectors(page)
    _add_custom(page, "cli", "Stub CLI")
    page.fill("[data-testid=custom-command]", "stubcli")
    page.fill("[data-testid=custom-cli-login]", "login")
    page.fill("[data-testid=custom-cli-test]", "status --json")
    page.click("[data-testid=save-custom-connector]")
    page.wait_for_selector("[data-testid=cli-connection]")
    page.click("[data-testid=test-connection]")
    page.wait_for_selector("[data-testid=connection-message]:has-text('not signed in')")

    page.click("[data-testid=cli-sign-in]")
    page.wait_for_selector("[data-testid=connection-status][data-state=working]", timeout=30_000)

    page.click("[data-testid=add-command-toggle]")
    page.fill("[data-testid=command-name]", "greet")
    page.fill("[data-testid=command-words]", "greet {name}")
    page.fill("[data-testid=command-description]", "Greet someone by name.")
    page.click("[data-testid=save-command]")
    page.wait_for_selector("[data-testid=tool-row][data-tool=greet][data-permission=ask]")
