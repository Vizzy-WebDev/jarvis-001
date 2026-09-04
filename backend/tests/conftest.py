"""Shared fixtures for the port's test suite.

The most valuable tests here compare the Python implementation against the REAL
Node implementation still living in server/, rather than against expectations
this port wrote down for itself. A test that only checks the port against its own
assumptions cannot catch a misreading of the original — which, for a rewrite whose
entire job is behavioural equality, is the failure mode that matters.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def node_available() -> bool:
    return shutil.which("node") is not None and (REPO_ROOT / "server" / "db.js").exists()


requires_node = pytest.mark.skipif(
    not node_available(),
    reason="needs the Node implementation in server/ to compare against",
)


def run_node(script: str, data_dir: Path | None = None, env_path: Path | None = None) -> str:
    """Run a snippet against the real Node app and return its stdout.

    Environment overrides are passed through the child's real environment, never
    interpolated into the -e string: a POSIX-style scratch path embedded in a JS
    string literal is mangled by Node's own parsing on Windows, and a shell
    variable referenced inside -e expands before any command-prefix assignment
    applies (see root CLAUDE.md).
    """
    env = dict(os.environ)
    if data_dir is not None:
        env["JARVIS_DATA_DIR"] = str(data_dir)
    if env_path is not None:
        env["JARVIS_ENV_PATH"] = str(env_path)
    proc = subprocess.run(
        ["node", "--experimental-sqlite", "-e", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return proc.stdout


def node_migrate(data_dir: Path) -> None:
    """Build/complete a database using the real Node migration runner."""
    run_node("import('./server/db.js').then(m => { m.getDb(); })", data_dir=data_dir)


def schema_fingerprint(db_path: Path) -> list[tuple[str, str, str]]:
    """Every schema object, whitespace-normalised, ordered — the exact thing that
    must match between the two implementations."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        return [(t, n, " ".join((s or "").split())) for t, n, s in rows]
    finally:
        conn.close()


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """An isolated data dir + .env, wired through the same overrides the Node app
    uses. Testing must never touch the user's real data/, .env or port."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = tmp_path / ".env"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JARVIS_ENV_PATH", str(env_path))
    # A key inherited from the real environment would mask a file-read bug.
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    from jarvis import db as db_module
    from jarvis import store as store_module

    db_module.reset_for_tests()
    store_module.reset_for_tests()
    yield type("Scratch", (), {"data_dir": data_dir, "env_path": env_path})()
    db_module.reset_for_tests()
    store_module.reset_for_tests()
