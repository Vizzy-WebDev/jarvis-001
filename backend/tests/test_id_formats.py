"""Generated ids must keep the shape already stored.

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

from jarvis.jscompat import base36, compact_json, now_iso

# `c` + base36(Date.now()) + 6 base36 random chars.
CONVERSATION_ID = re.compile(r"^c[0-9a-z]{8,9}[0-9a-z]{6}$")

def test_conversation_id_shape(scratch):
    from jarvis import chat_store

    conv = chat_store.create_conversation()
    assert CONVERSATION_ID.match(conv["id"]), f"unexpected id shape: {conv['id']!r}"

def test_message_id_shape(scratch):
    from jarvis import chat_store

    conv = chat_store.create_conversation()
    chat_store.append_message(conv["id"], {"role": "user", "text": "hi"})
    message = chat_store.get_messages(conv["id"])[0]
    assert re.match(r"^m\d+$", message["id"]), message["id"]

def test_timestamp_shape():
    """The stored timestamp format — exactly three fractional digits, trailing Z.

    Python's own isoformat() gives six digits and '+00:00', which would sort and
    compare differently against every stored row.
    """
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", now_iso())

