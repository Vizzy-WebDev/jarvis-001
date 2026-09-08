"""Differential test: run the same scenario through both implementations.

The contract fixtures cover the HTTP surface. This covers the layer underneath
it — the store functions themselves, including the paths no route exercises
directly (FTS query construction, payload encoding, the search fallback).

Both sides run the SAME scripted sequence against their own fresh database and
print a JSON transcript of every observable result. The test passes only if the
two transcripts are identical. That is a stronger claim than "the port matches
what I believed the original did", and it is the only kind of claim worth making
for a rewrite whose entire purpose is behavioural equality.
"""

from __future__ import annotations

import json
import subprocess
import sys

from conftest import REPO_ROOT, requires_node

SCENARIOS = [("chat_store_scenario.mjs", "chat_store_scenario.py")]


def _run(cmd: list[str], data_dir, cwd=REPO_ROOT) -> str:
    import os

    env = dict(os.environ)
    env["JARVIS_DATA_DIR"] = str(data_dir)
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{cmd[0]} scenario failed:\n{proc.stderr[:4000]}")
    return proc.stdout


@requires_node
def test_chat_store_behaves_identically_to_node(tmp_path):
    here = REPO_ROOT / "backend" / "tests" / "parity"
    node_out = _run(
        ["node", "--experimental-sqlite", str(here / "chat_store_scenario.mjs")],
        tmp_path / "node",
    )
    py_out = _run(
        [sys.executable, str(here / "chat_store_scenario.py")],
        tmp_path / "py",
    )

    node_result = json.loads(node_out)
    py_result = json.loads(py_out)

    # Compare step by step so a failure names the operation that diverged rather
    # than dumping two large blobs.
    assert len(node_result) == len(py_result), "scenarios ran a different number of steps"
    for node_step, py_step in zip(node_result, py_result):
        assert node_step == py_step, f"diverged at step {node_step[0]!r}"
