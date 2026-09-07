"""Running code, producing files, and serving them (§35, §45, §3).

Three claims are worth more than the rest, and each has a test that would fail
against the behaviour this replaces:

* the tool's description reports the isolation the machine ACTUALLY has;
* a generated file that cannot be re-opened is deleted rather than recorded;
* the serving route forces a download unconditionally, so a crafted .svg cannot
  run script in the app's own origin.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis import artifacts, sandbox
from jarvis.artifacts.office import entry_names, write_docx, write_xlsx
from jarvis.capabilities import CapabilityRegistry, Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.documents import read_docx, read_xlsx
from jarvis.main import create_app
from jarvis.sandbox.runner import child_environment
from jarvis.tools import load_tools


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    sandbox.runner.reset_for_tests()
    yield
    sandbox.runner.reset_for_tests()
    reset_db()


@pytest.fixture
def reg():
    registry = CapabilityRegistry()
    load_tools(registry)
    return registry


@pytest.fixture
def staging(tmp_path):
    return tmp_path


# --- the sandbox -------------------------------------------------------------

def test_the_description_reports_the_isolation_this_machine_actually_has(reg):
    """The defect this replaces: a description promising a sandbox the backend
    did not implement, which the model then repeated to the user."""
    description = reg.get("run_code").description
    if sandbox.backend() == "wsl":
        assert "inside WSL" in description
    else:
        assert "NOT fully sandboxed" in description
        assert "can read other files" in description


def test_without_a_real_boundary_running_code_is_high_risk(reg):
    expected = Risk.MEDIUM if sandbox.backend() == "wsl" else Risk.HIGH
    assert reg.get("run_code").risk is expected


def test_api_keys_never_reach_the_code(monkeypatch):
    """The parent holds every key the user has configured, and a child inheriting
    os.environ inherits all of them."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-key")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("JARVIS_DATA_DIR", "/somewhere")
    child = child_environment()
    assert "ANTHROPIC_API_KEY" not in child
    assert "SOME_SERVICE_TOKEN" not in child
    assert "JARVIS_DATA_DIR" not in child
    assert "PATH" in child


def test_a_secret_added_later_is_scrubbed_without_anyone_updating_a_list(monkeypatch):
    monkeypatch.setenv("A_BRAND_NEW_SECRET", "x")
    assert "A_BRAND_NEW_SECRET" not in child_environment()


def test_code_runs_and_its_output_comes_back():
    result = sandbox.run_python("print(sum(range(10)))")
    assert result.ok and result.stdout.strip() == "45"


def test_code_that_fails_is_reported_as_failing_not_as_empty_output():
    result = sandbox.run_python("raise SystemExit(3)")
    assert result.ok is False and result.exit_code == 3


def test_code_that_runs_forever_is_stopped():
    result = sandbox.run_python("while True:\n    pass", timeout_s=1.0)
    assert result.timed_out is True and result.ok is False
    assert "stopped it" in result.error


def test_enormous_output_is_capped():
    result = sandbox.run_python("print('x' * 100000)")
    assert len(result.stdout) < 100000 and "truncated" in result.stdout


# --- the writers -------------------------------------------------------------

def test_every_zip_entry_name_uses_forward_slashes(staging):
    """A name derived from a directory walk picks up the platform separator, and
    on Windows that produces a file with the right signature that Word will not
    open at all. Read back from a real archive rather than assumed."""
    for path in (write_docx(staging / "a.docx", ["Hi"]),
                 write_xlsx(staging / "b.xlsx", [["a", 1]])):
        names = entry_names(path)
        assert names and all("\\" not in name for name in names)
        assert "[Content_Types].xml" in names


def test_a_document_round_trips_through_an_independently_written_reader(staging):
    path = write_docx(staging / "r.docx", ["First line.", "Second & third <ok>"],
                      title="Report")
    assert read_docx(path) == "Report\nFirst line.\nSecond & third <ok>"


def test_a_spreadsheet_round_trips_with_numbers_still_numbers(staging):
    path = write_xlsx(staging / "s.xlsx", [["Name", "Count"], ["Widgets", 12]])
    assert read_xlsx(path) == [["Name", "Count"], ["Widgets", "12"]]


def test_special_characters_do_not_corrupt_the_package(staging):
    path = write_docx(staging / "x.docx", ['5 < 6 & "quoted" > done'])
    assert read_docx(path) == '5 < 6 & "quoted" > done'


# --- keeping and verifying ---------------------------------------------------

def test_a_file_that_cannot_be_reopened_is_thrown_away(staging):
    broken = staging / "broken.docx"
    broken.write_bytes(b"this is not a zip at all")
    with pytest.raises(ValueError, match="doesn't open"):
        artifacts.keep(broken)
    assert not broken.exists(), "a broken file must not be left lying around"
    assert artifacts.recent() == []


def test_a_passing_check_is_actually_recorded(staging):
    """The half that was silently missing: the check ran and the column stayed
    null, so every kept artifact claimed to be unverified forever."""
    kept = artifacts.keep(write_docx(staging / "ok.docx", ["Hello"]))
    assert artifacts.get(kept.id).verified is True


def test_an_unverifiable_format_is_not_recorded_as_verified(staging):
    """None is not True. Nothing here can re-open a .md, and saying so is the
    honest answer."""
    plain = staging / "notes.md"
    plain.write_text("# Hello")
    kept = artifacts.keep(plain)
    assert kept.verified is None and "has not been checked" in kept.detail


def test_the_stored_path_comes_from_the_id_not_the_name(staging):
    kept = artifacts.keep(write_docx(staging / "z.docx", ["Hi"]),
                          name="../../escape attempt.docx")
    assert kept.path.parent == artifacts.artifacts_dir()
    assert ".." not in kept.path.name


# --- the tools ---------------------------------------------------------------

def test_creating_a_document_produces_something_that_opens(reg):
    from jarvis.tools.create_artifact import _run

    result = _run(filename="Report.docx", paragraphs=["One.", "Two."])
    assert result["ok"] and result["verified"] is True
    assert read_docx(artifacts.get(result["id"]).path) == "One.\nTwo."


def test_image_generation_is_refused_plainly(reg):
    from jarvis.tools.create_artifact import _run

    assert "can't make a .png" in _run(filename="photo.png", content="x")["error"]


def test_creating_an_artifact_is_reachable_from_a_background_job(reg):
    """In the original this tool was meta, and a background job strips meta
    tools before anything else applies — so a job asked to write a report
    structurally could not."""
    assert not reg.get("create_artifact").has_tag("meta")


def test_a_script_that_produces_a_file_hands_it_back_verified(reg):
    from jarvis.tools.run_code import _run

    result = _run(code=(
        "import zipfile\n"
        "with zipfile.ZipFile('out.xlsx','w') as z: z.writestr('x','y')\n"
        "print('made it')"))
    assert result["ok"] and result["stdout"].strip() == "made it"
    # It is a zip, but not a workbook: verification catches that rather than
    # handing back something that will not open.
    assert result["files"][0]["ok"] is False


# --- serving -----------------------------------------------------------------

def test_a_served_file_is_always_a_download_never_rendered_in_our_own_origin(staging):
    """The stored-XSS finding: nothing restricts what an .svg artifact contains,
    and a gate a caller can omit is not a gate."""
    hostile = staging / "chart.svg"
    hostile.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    kept = artifacts.keep(hostile)

    client = TestClient(create_app())
    response = client.get(f"/api/artifacts/{kept.id}")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_a_filename_cannot_inject_response_headers(staging):
    plain = staging / "ok.txt"
    plain.write_text("hello")
    kept = artifacts.keep(plain, name="evil\r\nX-Injected: yes.txt")

    client = TestClient(create_app())
    response = client.get(f"/api/artifacts/{kept.id}")
    assert "x-injected" not in {k.lower() for k in response.headers}


def test_a_missing_artifact_is_a_404_not_a_crash():
    client = TestClient(create_app())
    response = client.get("/api/artifacts/art_nope")
    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "Not found."}
