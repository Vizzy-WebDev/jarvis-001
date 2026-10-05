"""`Start Jarvis.bat`, read rather than run.

The launcher is a Windows batch file and this suite also runs where there is no `cmd.exe`, so
what can honestly be checked here is its structure: that an existing install notices when
`pyproject.toml` changed and installs again — an update that adds a package (LangGraph,
for durable background work) must not leave an existing install unable to start. Running it
for real is a check on Windows.
"""

from __future__ import annotations

import re
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "Start Jarvis.bat"


def _lines() -> list[str]:
    return [line.strip() for line in LAUNCHER.read_text(encoding="utf-8").splitlines()]


def _index(lines: list[str], pattern: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if re.search(pattern, lines[i], re.IGNORECASE):
            return i
    raise AssertionError(f"not found after line {start}: {pattern}")


def test_a_first_install_records_what_it_installed():
    lines = _lines()
    install = _index(lines, r'"%VENV_PY%" -m pip install -e \.')
    failed = _index(lines, r"if errorlevel 1", install)
    stamp = _index(lines, r'copy /y "pyproject\.toml" "%VENV%\\installed-pyproject\.toml"',
                   install)
    # Recorded only after the install succeeded (the failure branch exits first).
    assert failed < stamp
    assert "exit /b 1" in " ".join(lines[failed:stamp])


def test_every_launch_installs_again_when_the_package_list_changed():
    lines = _lines()
    compare = _index(lines, r'fc /b "pyproject\.toml" "%STAMP%"')
    assert re.search(r"if errorlevel 1 set \"REINSTALL=1\"", lines[compare + 1], re.IGNORECASE)
    missing = _index(lines, r'if not exist "%STAMP%" set "REINSTALL=1"')
    assert missing < compare
    reinstall = _index(lines, r"if defined REINSTALL", compare)
    install = _index(lines, r'"%VENV_PY%" -m pip install -e \.', reinstall)
    failed = _index(lines, r"if errorlevel 1", install)
    refresh = _index(lines, r'copy /y "pyproject\.toml" "%STAMP%"', install)
    assert failed < refresh, "the record must be refreshed only after a successful install"
    # ...and all of it before the server is started.
    assert refresh < _index(lines, r"Starting the Jarvis server")


def test_its_messages_stay_in_plain_language():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "Updating what Jarvis needs for this version" in text
    assert "Jarvis couldn't install what this version needs" in text
    assert "Traceback" not in text and "pip" not in " ".join(
        line for line in text.splitlines() if line.strip().lower().startswith("echo"))
