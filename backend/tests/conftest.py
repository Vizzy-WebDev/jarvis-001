"""Shared fixtures for the test suite.

The durable record of the API's behaviour is tests/contract/fixtures/: 45 recorded HTTP
exchanges, replayed by test_contract.py, so the behaviour the API promises has a witness.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """An isolated data dir + .env, wired through the standard overrides
    (`JARVIS_DATA_DIR`, `JARVIS_ENV_PATH`, `PORT`). Testing must never touch the user's real data/, .env or port."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = tmp_path / ".env"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JARVIS_ENV_PATH", str(env_path))
    from jarvis import config as config_module

    # A key inherited from the real environment would mask a file-read bug.
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # `save_secret` writes the .env file AND os.environ, so a secret saved by one
    # test is still in the process for the next one — a scratch .env alone does
    # NOT isolate them. Found by a test that added a service, was refused for
    # colliding with a name the previous test had used, and reported the wrong
    # cause. Cleared here rather than in each test that happens to notice.
    for var in [name for name in os.environ if name.startswith(config_module.SECRET_PREFIX)]:
        monkeypatch.delenv(var, raising=False)

    from jarvis import db as db_module
    from jarvis import session as session_module
    from jarvis import store as store_module

    # The active conversation id is cached in a module global keyed to whichever
    # database was open. Leaving it set across a scratch boundary makes the next
    # test resolve "the conversation the user has open" to a row in a database
    # that is gone — which presents as a route quietly returning nothing.
    db_module.reset_for_tests()
    store_module.reset_for_tests()
    session_module.reset_for_tests()
    yield type("Scratch", (), {"data_dir": data_dir, "env_path": env_path})()
    db_module.reset_for_tests()
    store_module.reset_for_tests()
    session_module.reset_for_tests()


@pytest.fixture
def live_server(scratch):
    """The real app on a real socket, on an unusual port.

    Some behaviour is only observable over a genuine connection — a stream that
    ends when the client disconnects cannot be exercised by an in-process client
    that never closes a socket. Never port 3000, and never the user's data dir:
    `scratch` is a prerequisite, not a suggestion.
    """
    import socket
    import threading
    import time

    import uvicorn

    from jarvis.main import create_app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("the test server never started")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


