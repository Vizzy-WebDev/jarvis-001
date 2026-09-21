"""store.py and config.py must keep the on-disk formats stable.

Files already in data/ and an existing .env must keep loading, so "close enough" is not a
passing grade: a difference in JSON indentation or .env line format shows up as a spurious
file change, and an ops config-integrity check that hashes file contents would flag it as
tampering.
"""

from __future__ import annotations

import json

from jarvis import config, store

# --- store.py ---------------------------------------------------------------

SAMPLE = {
    "b": 1,
    "a": ["x", "é", None, True, False],
    "nested": {"deep": {"k": "v"}},
    "empty_obj": {},
    "empty_arr": [],
    "num": 1.5,
    "unicode": "naïve — ok",
}

def test_write_json_round_trips(scratch):
    store.write_json("demo", SAMPLE)
    assert store.read_json("demo") == SAMPLE
    assert store.exists("demo")

def test_read_json_falls_back_when_missing_or_corrupt(scratch):
    assert store.read_json("nope", {"fallback": True}) == {"fallback": True}
    (scratch.data_dir / "broken.json").write_text("{not json", encoding="utf-8")
    assert store.read_json("broken", "fell-back") == "fell-back"

def test_write_is_atomic_leaving_no_temp_files(scratch):
    store.write_json("demo", SAMPLE)
    leftovers = [p.name for p in scratch.data_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []

def test_last_write_records_a_content_hash(scratch):
    assert store.last_write_at("demo") is None
    store.write_json("demo", SAMPLE)
    record = store.last_write_at("demo")
    assert record is not None and len(record["hash"]) == 64

def test_data_dir_honours_the_override(scratch):
    # A module hardcoding a path relative to its own source file bypasses test
    # isolation entirely — the real bug that put a 145MB browser profile into
    # the user's actual data/ during a scratch run.
    assert store.data_dir() == scratch.data_dir

# --- config.py --------------------------------------------------------------

def test_env_round_trips(scratch):
    config.save_secret("deepgram", "dg-456")
    assert config.get_secret("deepgram") == "dg-456"

def test_delete_secret_drops_the_line_entirely(scratch):
    config.save_secret("deepgram", "dg-456")
    config.delete_secret("deepgram")
    assert config.get_secret("deepgram") is None
    assert "DEEPGRAM" not in scratch.env_path.read_text(encoding="utf-8")

def test_quoted_values_are_unquoted_and_comments_skipped(scratch):
    scratch.env_path.write_text(
        '# a comment\nJARVIS_SECRET_DEEPGRAM="quoted-key"\nBAD_LINE_NO_EQUALS\n\n',
        encoding="utf-8",
    )
    assert config.get_secret("deepgram") == "quoted-key"

