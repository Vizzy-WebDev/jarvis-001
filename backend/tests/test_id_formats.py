"""Generated ids must keep the shape the Node implementation produces.

The contract harness normalises id values away, because they differ every run.
That is necessary and also dangerous: normalising a value hides its FORMAT too,
so a port that emitted "conv-1", a UUID, or an empty string would sail straight
through the replay suite.

These tests pin the format independently, so the two protections do not overlap
into a blind spot. Ids reach the database, the front end and the URL bar, and the
two implementations run against the same data during the migration — an id that
sorts or parses differently is a real defect.
"""

from __future__ import annotations

import re
import subprocess

from conftest import REPO_ROOT, requires_node

from jarvis.jscompat import base36, compact_json, now_iso

# `c` + base36(Date.now()) + 6 base36 random chars.
CONVERSATION_ID = re.compile(r"^c[0-9a-z]{8,9}[0-9a-z]{6}$")


def test_conversation_id_shape(scratch):
    from jarvis import chat_store

    conv = chat_store.create_conversation()
    assert CONVERSATION_ID.match(conv["id"]), f"unexpected id shape: {conv['id']!r}"


@requires_node
def test_conversation_id_shape_matches_node(scratch, tmp_path):
    """The strongest form of the check: generate one with each implementation and
    require the same shape, rather than trusting a regex written from reading."""
    from jarvis import chat_store

    ours = chat_store.create_conversation()["id"]

    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    import os

    env = dict(os.environ)
    env["JARVIS_DATA_DIR"] = str(node_dir)
    proc = subprocess.run(
        [
            "node",
            "--experimental-sqlite",
            "-e",
            "import('./server/chat-store.js').then(m => process.stdout.write(m.createConversation().id))",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    theirs = proc.stdout.strip()

    assert len(ours) == len(theirs), f"id length differs: {ours!r} vs {theirs!r}"
    assert ours[0] == theirs[0] == "c"
    assert CONVERSATION_ID.match(theirs), f"the regex does not describe Node's own id: {theirs!r}"


def test_message_id_shape(scratch):
    from jarvis import chat_store

    conv = chat_store.create_conversation()
    chat_store.append_message(conv["id"], {"role": "user", "text": "hi"})
    message = chat_store.get_messages(conv["id"])[0]
    assert re.match(r"^m\d+$", message["id"]), message["id"]


def test_timestamp_shape():
    """`new Date().toISOString()` — exactly three fractional digits, trailing Z.

    Python's own isoformat() gives six digits and '+00:00', which would sort and
    compare differently against every row the Node app wrote.
    """
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", now_iso())


@requires_node
def test_base36_matches_node():
    cases = [0, 1, 35, 36, 1234567890123, 999999, 2**53 - 1]
    proc = subprocess.run(
        ["node", "-e", f"process.stdout.write(JSON.stringify({cases}.map(n => n.toString(36))))"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == compact_json([base36(n) for n in cases])
