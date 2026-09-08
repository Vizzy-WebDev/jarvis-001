"""HTTP-level differential tests: the same request sequence, both servers.

Boots the REAL Node server on an unusual scratch port with an isolated data
directory, runs a scenario against it, then runs the identical scenario against
the FastAPI app in-process, and requires the two transcripts to match step for
step.

This is the check that covers mutating routes, which fixture replay structurally
cannot: a recorded PATCH names an id that only existed during the recording.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from typing import Any

import pytest
from conftest import REPO_ROOT, requires_node
from parity.http_scenarios import SCENARIOS

# An unusual port, per the project's own testing rules — never near the port the
# owner's real instance uses.
NODE_TEST_PORT = 39231


def _free_port(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture(scope="module")
def node_server(tmp_path_factory):
    """The real Node app, on a scratch port with an isolated data dir and .env."""
    if not _free_port(NODE_TEST_PORT):
        pytest.skip(f"port {NODE_TEST_PORT} is already in use")

    data_dir = tmp_path_factory.mktemp("node-parity-data")
    env_path = tmp_path_factory.mktemp("node-parity-env") / ".env"
    env_path.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "PORT": str(NODE_TEST_PORT),
            "JARVIS_DATA_DIR": str(data_dir),
            "JARVIS_ENV_PATH": str(env_path),
            # These two do NOT honour JARVIS_DATA_DIR in the Node app — see
            # docs/migration/findings.md. Set explicitly so this test cannot
            # write screenshots or recordings into the real data directory.
            "JARVIS_RECORDINGS_DIR": str(data_dir / "recordings"),
            "JARVIS_SCREENSHOTS_DIR": str(data_dir / "screenshots"),
        }
    )
    proc = subprocess.Popen(
        ["node", "--experimental-sqlite", "server/server.js"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    import httpx

    base = f"http://127.0.0.1:{NODE_TEST_PORT}"
    for _ in range(100):
        if proc.poll() is not None:
            pytest.fail(f"node server exited early:\n{proc.stdout.read()[:4000]}")
        try:
            httpx.get(f"{base}/api/status", timeout=1.0)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("node server never came up")

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _node_requester(base: str):
    import httpx

    client = httpx.Client(base_url=base, timeout=30.0)

    def request(method: str, path: str, payload: Any = None) -> tuple[int, Any]:
        response = client.request(method, path, json=payload)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"_text": response.text}

    return request


def _python_requester(tmp_path):
    os.environ["JARVIS_DATA_DIR"] = str(tmp_path / "py-parity-data")
    os.environ["JARVIS_ENV_PATH"] = str(tmp_path / "py-parity.env")
    (tmp_path / "py-parity-data").mkdir(parents=True, exist_ok=True)

    from fastapi.testclient import TestClient

    from jarvis import chat_store, conversation, session, session_hooks  # noqa: F401
    from jarvis import db as db_module
    from jarvis import store as store_module
    from jarvis.main import create_app

    db_module.reset_for_tests()
    store_module.reset_for_tests()
    conversation.reset_for_tests()
    session.reset_for_tests()

    client = TestClient(create_app())

    def request(method: str, path: str, payload: Any = None) -> tuple[int, Any]:
        response = client.request(method, path, json=payload)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"_text": response.text}

    return request


@requires_node
@pytest.mark.parametrize("scenario_name", sorted(SCENARIOS))
def test_http_scenario_matches_node(node_server, tmp_path, scenario_name):
    scenario = SCENARIOS[scenario_name]

    node_steps = scenario(_node_requester(node_server))
    python_steps = scenario(_python_requester(tmp_path))

    assert len(node_steps) == len(python_steps), "scenarios ran a different number of steps"
    for node_step, python_step in zip(node_steps, python_steps):
        assert node_step == python_step, (
            f"diverged at step {node_step['step']!r}\n"
            f"  node:   {node_step}\n"
            f"  python: {python_step}"
        )
