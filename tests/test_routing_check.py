"""The live routing check judges honestly, and can never act on the person's accounts."""

from __future__ import annotations

import os

import pytest

from jarvis import routing_check as rc
from jarvis.routing_check import Reply, Scenario


def _call(name, result=None):
    return {"name": name, "args": {}, "result": result}


# --- the safety property: no connector sign-in ever reaches the scratch folder ---------------------

def test_the_scratch_folder_has_the_model_keys_and_no_connector_sign_in(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    (real / "connectors.json").write_text("{}", encoding="utf-8")
    (real / "models.yaml").write_text("connections: []", encoding="utf-8")
    env = tmp_path / "real.env"
    env.write_text("\n".join([
        "JARVIS_SECRET_MODEL_GEMINI=model-key",
        "JARVIS_SECRET_CONNOAUTH_ABC=oauth-token-set",
        "jarvis_secret_connclient_abc=client-secret",      # case must not matter
        "JARVIS_SECRET_CONN_XYZ=api-key",
        "JARVIS_SECRET_CONNCLI_X_FOO=cli-key",
        "JARVIS_SECRET_ELEVENLAB=voice-key",
    ]) + "\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data-before"))
    monkeypatch.setenv("JARVIS_ENV_PATH", str(tmp_path / "env-before"))

    scratch = rc.prepare_scratch(real, tmp_path / "scratch", env_file=env)

    copied = (scratch / ".env").read_text(encoding="utf-8")
    assert "model-key" in copied and "voice-key" in copied
    for secret in ("oauth-token-set", "client-secret", "api-key", "cli-key"):
        assert secret not in copied
    assert (scratch / "data" / "connectors.json").exists()      # the tool LISTS are copied
    assert os.environ["JARVIS_DATA_DIR"] == str(scratch / "data")


def test_it_refuses_to_run_inside_the_real_data(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    with pytest.raises(SystemExit):
        rc.prepare_scratch(real, real / "inside")


# --- the judging ----------------------------------------------------------------------------------

def test_using_an_app_after_being_told_not_to_is_flagged_even_if_only_asked_about():
    scenario = Scenario("x", "...", (("avoid", "higgsfield"),))
    assert rc.problems_for(Reply("...", "ok", cards=["higgsfield__generate_image"]), scenario)
    assert rc.problems_for(Reply("...", "ok", calls=[_call("higgsfield__generate_image")]), scenario)
    assert not rc.problems_for(Reply("...", "ok", calls=[_call("canva__generate_image")]), scenario)


def test_the_search_still_offering_the_excluded_app_is_flagged():
    scenario = Scenario("x", "...", (("avoid", "higgsfield"),))
    leak = Reply("...", "ok", calls=[_call("find_capability", {"unlock": ["higgsfield__generate_image"]})])
    assert any("search" in p for p in rc.problems_for(leak, scenario))


def test_a_named_app_must_actually_be_used():
    scenario = Scenario("x", "...", (("uses", "canva"),))
    assert rc.problems_for(Reply("...", "done"), scenario)
    assert not rc.problems_for(Reply("...", "done", cards=["canva__create_design"]), scenario)


def test_a_model_nothing_offers_must_be_acknowledged_not_swapped():
    scenario = Scenario("x", "...", (("acknowledges", "veo"),))
    assert rc.problems_for(Reply("...", "Here is your video!", calls=[_call("higgsfield__generate_video")]), scenario)
    assert not rc.problems_for(Reply("...", "I can't find Veo here. Want me to use another?"), scenario)


def test_an_app_not_set_up_must_be_said_to_be_and_not_done_through_an_aggregator():
    scenario = Scenario("x", "...", (("says_not_set_up", "github"), ("no_detour", "composio")))
    good = Reply("...", "GitHub isn't set up in Connector yet. Want to connect it?")
    assert not rc.problems_for(good, scenario)
    assert rc.problems_for(Reply("...", "Done, I made the issue."), scenario)
    assert rc.problems_for(Reply("...", "GitHub isn't set up.", calls=[_call("composio__composio_search_tools")]), scenario)


def test_a_failed_turn_is_reported_not_passed():
    assert rc.problems_for(Reply("...", failed="boom"), Scenario("x", "...", (("uses", "a"),)))


def test_doing_one_job_through_several_apps_that_worked_is_flagged():
    scenario = Scenario("x", "...", (("one_app_only",),))
    ok = {"ok": True, "result": "Image ready"}
    many = Reply("...", "done", calls=[_call("pixelforge__generate_image", ok), _call("artisan__make_picture_create", ok)])
    assert any("several apps" in p for p in rc.problems_for(many, scenario))
    one = Reply("...", "done", calls=[_call("find_capability"), _call("canva__generate_image", ok)])
    assert not rc.problems_for(one, scenario)


def test_falling_back_after_a_failure_is_not_a_duplicate():
    """The mistake this check once made: three apps tried in turn because each said not connected."""
    scenario = Scenario("x", "...", (("one_app_only",),))
    failed = {"ok": False, "error": "generate_image didn't work: This connector is not connected yet."}
    chain = Reply("...", "sorry", calls=[_call("higgsfield__generate_image", failed),
                                         _call("canva__generate_image", failed)])
    assert not rc.problems_for(chain, scenario)
    # ...but a fallback that then WORKED, after another also worked, is
    both = Reply("...", "done", calls=[_call("higgsfield__generate_image", {"ok": True}),
                                      _call("canva__generate_image", {"ok": True})])
    assert rc.problems_for(both, scenario)
