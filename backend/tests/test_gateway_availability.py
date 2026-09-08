"""The unified availability store.

This replaces three overlapping mechanisms, so the tests are about the
PROPERTIES that were previously inconsistent — one vocabulary, one cooldown per
state, survives a restart, and an explanation that cannot disagree with the
filter that produced it.
"""

from __future__ import annotations

import pytest

from jarvis.gateway import availability as av


@pytest.fixture(autouse=True)
def _isolated(scratch):
    av.reset_for_tests()
    yield
    av.reset_for_tests()


def test_unknown_model_is_eligible():
    assert av.is_eligible("never-seen") is True
    assert av.retry_after_ms("never-seen") == 0


def test_a_failure_benches_a_model_for_its_state_cooldown():
    av.record("m1", "quota", detail="Out of quota for today.")
    assert av.is_eligible("m1") is False
    assert av.status_of("m1")["state"] == "quota"
    # Just before the cooldown expires it is still benched; just after, eligible.
    now = av.status_of("m1")["since"]
    assert av.is_eligible("m1", now + av.COOLDOWNS_MS["quota"] - 1) is False
    assert av.is_eligible("m1", now + av.COOLDOWNS_MS["quota"]) is True


def test_success_clears_the_record_entirely():
    av.record("m1", "auth")
    assert av.is_eligible("m1") is False
    av.record("m1", av.WORKING)
    assert av.status_of("m1") is None
    assert av.is_eligible("m1") is True


def test_every_state_has_exactly_one_cooldown():
    """The defect this store exists to remove: the Node version had two tables
    holding different values for the same failure, so the exclusion reason
    depended on how long the process had been running."""
    assert set(av.COOLDOWNS_MS) == {
        "quota", "auth", "no_access", "busy", "unsupported", "unreachable", "error",
    }
    assert all(v > 0 for v in av.COOLDOWNS_MS.values())


def test_an_unclassified_failure_gets_a_moderate_cooldown_not_the_harshest():
    """`error` must not inherit `auth`'s 6-hour penalty just because nothing
    matched — that was the behaviour before `error` got its own state."""
    assert av.COOLDOWNS_MS["error"] < av.COOLDOWNS_MS["auth"]
    assert av.COOLDOWNS_MS["error"] < av.COOLDOWNS_MS["no_access"]


def test_a_provider_overload_clears_fast():
    """A 503 is not the model's fault and typically clears in seconds."""
    assert av.COOLDOWNS_MS["busy"] < av.COOLDOWNS_MS["quota"]


def test_state_survives_a_restart():
    """health.js's breaker was in-memory only, so a restart silently changed
    routing. This one is persisted."""
    av.record("m1", "quota")
    av.reset_for_tests()  # simulates a fresh process
    assert av.is_eligible("m1") is False
    assert av.status_of("m1")["state"] == "quota"


def test_explain_cannot_disagree_with_the_filter():
    av.record("m1", "quota", detail="Out of quota.")
    av.record("m2", "auth", detail="Key rejected.")
    result = av.explain(["m1", "m2", "m3"])

    # m3 is eligible, so it must not be explained as excluded.
    assert set(result["excluded"]) == {"m1", "m2"}
    for model_id in result["excluded"]:
        assert av.is_eligible(model_id) is False
    assert result["counts"] == {"quota": 1, "auth": 1}


def test_explain_reports_a_real_retry_estimate():
    """What lets an all-exhausted message give a number instead of an apology."""
    av.record("m1", "busy")
    av.record("m2", "auth")
    result = av.explain(["m1", "m2"])
    # The soonest return is the shortest cooldown among the benched models.
    assert 0 < result["soonestRetryMs"] <= av.COOLDOWNS_MS["busy"]


def test_clear_lets_a_user_retry_immediately():
    av.record("m1", "auth")
    assert av.is_eligible("m1") is False
    av.clear("m1")
    assert av.is_eligible("m1") is True


def test_availability_is_not_written_into_models_json(scratch):
    """The lost-update race: in the Node version, recording availability went
    through updateModel()'s read-modify-write of models.json, and three
    concurrent rechecks silently dropped results. Availability lives in its own
    file so a background health write can never clobber user-owned config."""
    from jarvis.store import write_json, read_json

    write_json("models", {"entries": [{"id": "m1", "label": "mine"}]})
    av.record("m1", "quota")
    assert read_json("models") == {"entries": [{"id": "m1", "label": "mine"}]}
    assert (scratch.data_dir / "model-availability.json").exists()


def test_concurrent_records_do_not_lose_each_other():
    """The property the read-modify-write of models.json could not provide."""
    import threading

    def worker(n: int) -> None:
        for i in range(20):
            av.record(f"m{n}-{i}", "quota")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for n in range(4):
        for i in range(20):
            assert av.status_of(f"m{n}-{i}") is not None, f"lost m{n}-{i}"


def test_a_provider_error_that_quotes_the_key_is_stored_redacted(scratch):
    """This file outlives the process and is written on every failure, so the
    guarantee belongs here rather than in each caller — including the one who
    has not been written yet."""
    from jarvis import config
    from jarvis.redact import MASK

    config.save_secret("gemini", "AIzaTHISISTHEREALKEY99")
    av.record("m1", "auth",
              detail="Your API key AIzaTHISISTHEREALKEY99 is not valid.",
              technical='{"error":{"message":"API key not valid: AIzaTHISISTHEREALKEY99"}}')

    stored = av.status_of("m1")
    assert "AIzaTHISISTHEREALKEY99" not in stored["detail"]
    assert "AIzaTHISISTHEREALKEY99" not in stored["technical"]
    assert MASK in stored["detail"] and MASK in stored["technical"]
    # And not on disk either — the record is written through, not just cached.
    assert "AIzaTHISISTHEREALKEY99" not in \
        (scratch.data_dir / "model-availability.json").read_text(encoding="utf-8")
