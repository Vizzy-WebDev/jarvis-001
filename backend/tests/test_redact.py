"""Secrets never reach a file, a log, or the model.

A provider's raw error can quote the request that caused it. That text is
stored in data/model-availability.json, written to the log, and — for a
connector — handed back to the model and saved in the conversation. Every one of
those is somewhere a key must not be.
"""

from __future__ import annotations

import pytest

from jarvis import config
from jarvis.redact import MASK, redact, redact_text


@pytest.fixture
def env(scratch):
    """The scratch .env, with nothing inherited from the real one."""
    return scratch


def test_a_saved_key_is_replaced_wherever_it_appears(env):
    config.save_secret("gemini", "AIzaTOTALLYREALKEY123")
    error = ('400 from https://api/v1/models?key=AIzaTOTALLYREALKEY123: '
             'API key not valid (AIzaTOTALLYREALKEY123)')
    cleaned = redact(error)
    assert "AIzaTOTALLYREALKEY123" not in cleaned
    assert cleaned.count(MASK) == 2
    assert "API key not valid" in cleaned, "only the secret is removed, not the meaning"


def test_a_key_saved_under_a_generic_ref_is_replaced_too(env):
    config.save_secret("my_gateway", "hunter2-not-a-known-shape-at-all")
    assert redact("401: bad key hunter2-not-a-known-shape-at-all") == f"401: bad key {MASK}"


def test_a_key_only_in_the_env_file_is_still_found(env, monkeypatch):
    """A key saved before this process started is in the file and, until
    something reads it, nowhere else."""
    config.save_secret("openai", "sk-written-to-the-file-only-0001")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert "sk-written-to-the-file-only-0001" not in str(redact(
        "Incorrect API key provided: sk-written-to-the-file-only-0001"))


def test_a_key_shape_is_caught_even_when_nothing_was_saved(env):
    """The second line of defence: a key typed into a form and submitted, or set
    directly as an environment variable, was never saved here to be listed."""
    assert redact("auth failed for sk-ant-api03-abcdefghijklmnop") == f"auth failed for {MASK}"
    assert redact("bad key AIzaSyD-0123456789abcdef") == f"bad key {MASK}"


def test_an_unrelated_short_value_is_left_alone(env):
    """Node's four-character floor: a trivially short stored secret would
    otherwise blank out text wherever those characters happen to appear."""
    config.save_secret("tiny", "ab")
    assert redact("about half of that") == "about half of that"


def test_a_key_that_has_not_been_saved_yet_can_be_passed_in(env):
    """Testing a connection before saving it is exactly when a bad key produces
    an error that quotes it back."""
    assert redact("rejected: xyzzy-typed-just-now", extra=["xyzzy-typed-just-now"]) \
        == f"rejected: {MASK}"
    assert redact("rejected: xyzzy-typed-just-now", extra="xyzzy-typed-just-now") \
        == f"rejected: {MASK}"


def test_anything_that_is_not_text_comes_back_unchanged(env):
    assert redact(None) is None
    assert redact("") == ""
    assert redact({"a": 1}) == {"a": 1}
    assert redact_text(None) is None


def test_redaction_never_raises_even_if_the_secret_store_is_broken(env, monkeypatch):
    """A redaction that fails must not swallow the error it was cleaning up —
    and the shape pass still runs."""
    def explode() -> list[str]:
        raise OSError("no .env for you")

    monkeypatch.setattr("jarvis.redact.secret_values", explode)
    assert redact("failed with sk-abcdefghijklmnop") == f"failed with {MASK}"
