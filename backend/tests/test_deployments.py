"""Deployments: one model version reached through one connection.

The test this file exists for is `test_one_route_in_cooldown_leaves_its_sibling
_usable`. Keying availability on the version instead of the deployment reads
more naturally — the model is what failed — and would pass every other test
here. It would also mean a rate-limited gateway route taking the same model
offline when reached with your own key, which is the exact failure the crossing
axis exists to prevent.

The rest cover what the flat row could not express at all: the same model twice,
reached two ways, with separate histories.
"""

from __future__ import annotations

import pytest

from jarvis.catalog import Effort, EffortKind, Source, Support
from jarvis.gateway import availability, connections, deployments, effort


@pytest.fixture(autouse=True)
def _isolated(scratch):
    availability.reset_for_tests()
    effort.reset_for_tests()
    yield
    availability.reset_for_tests()
    effort.reset_for_tests()


def _connection(label: str, **kw):
    defaults = dict(adapter="openai-compatible", base_url=f"https://{label}.example",
                    label=label, provider="custom", kind="gateway", key_required=True,
                    secret="sk-not-a-real-key-000")
    defaults.update(kw)
    return connections.add_connection(**defaults)


# --- the reason this layer exists ------------------------------------------

def test_the_same_model_can_be_reached_through_two_connections():
    """Impossible to express as a flat row keyed on the model.

    Direct with your own key, and resold by a gateway: one model, two prices,
    two rate limits, two sets of things that can go wrong.
    """
    direct = _connection("direct")
    gateway = _connection("gateway")

    a = deployments.add_deployment(connection_id=direct["id"], model="shared-model")
    b = deployments.add_deployment(connection_id=gateway["id"], model="shared-model")

    assert a["id"] != b["id"]
    assert a["model"] == b["model"] == "shared-model"
    assert {d["id"] for d in deployments.list_deployments()} == {a["id"], b["id"]}


def test_one_route_in_cooldown_leaves_its_sibling_usable():
    """The keying test. If this fails, one bad route benches a good one.

    Written to fail loudly against the tempting simplification — keying
    availability on the model rather than on the pairing — which every other
    test in this file would tolerate.
    """
    direct = _connection("direct")
    gateway = _connection("gateway")
    a = deployments.add_deployment(connection_id=direct["id"], model="shared-model")
    b = deployments.add_deployment(connection_id=gateway["id"], model="shared-model")

    availability.record(b["id"], "quota", detail="the gateway is out of credit")

    assert availability.is_eligible(b["id"]) is False
    assert availability.is_eligible(a["id"]) is True, (
        "the same model through a different connection must be unaffected")


def test_a_refused_parameter_is_remembered_against_the_version_not_the_route():
    """The mirror image, and deliberately the opposite choice.

    Whether a model accepts a reasoning parameter is a fact about the MODEL, so
    learning it through one connection settles it for the other. Cooldowns are
    about the route; capabilities are about the thing at the end of it.
    """
    direct = _connection("direct")
    gateway = _connection("gateway")
    deployments.add_deployment(connection_id=direct["id"], model="claude-sonnet-5")
    b = deployments.add_deployment(connection_id=gateway["id"], model="claude-sonnet-5")

    effort.mark_unsupported(b["version"].provider, "claude-sonnet-5")

    assert effort.is_unsupported("anthropic", "claude-sonnet-5") is True


def test_uniqueness_is_the_pairing_not_the_model():
    conn = _connection("one")
    deployments.add_deployment(connection_id=conn["id"], model="dup")

    with pytest.raises(ValueError, match="already added"):
        deployments.add_deployment(connection_id=conn["id"], model="dup")


# --- hydration -------------------------------------------------------------

def test_a_deployment_cannot_drift_from_its_connection():
    """Nothing about the address is written down, so nothing about it can go
    stale — the one behaviour from the old registry worth keeping verbatim."""
    conn = _connection("movable")
    made = deployments.add_deployment(connection_id=conn["id"], model="m")
    assert made["baseUrl"] == "https://movable.example"

    connections.update_connection(conn["id"], {"baseUrl": "https://moved.example"})

    assert deployments.get_deployment(made["id"])["baseUrl"] == "https://moved.example"


def test_the_connections_provider_and_the_versions_provider_are_different_things():
    """A gateway set up as "custom" serving somebody else's model.

    Calling both of them `provider` is how the crossing axis quietly collapses
    back into one thing.
    """
    conn = _connection("aggregator", provider="custom")
    made = deployments.add_deployment(connection_id=conn["id"], model="claude-opus-5")

    assert made["connectionProvider"] == "custom"
    assert made["version"].provider == "anthropic"


def test_what_the_provider_reported_at_add_time_reaches_the_catalog():
    conn = _connection("c")
    made = deployments.add_deployment(
        connection_id=conn["id"], model="mystery",
        discovered={"context_tokens": 8000, "capabilities": {"tools": True}})

    assert made["version"].context_tokens == 8000
    assert made["version"].capabilities.tools is Support.YES
    assert made["version"].source_of("context_tokens") is Source.DISCOVERED


def test_a_user_correction_outranks_everything():
    conn = _connection("c")
    made = deployments.add_deployment(
        connection_id=conn["id"], model="claude-sonnet-5",
        discovered={"context_tokens": 111},
        overrides={"context_tokens": 999})

    assert made["version"].context_tokens == 999
    assert made["version"].source_of("context_tokens") is Source.USER


def test_a_correction_made_later_takes_effect_without_touching_stored_facts():
    conn = _connection("c")
    made = deployments.add_deployment(connection_id=conn["id"], model="unknown-thing")
    assert made["version"].family is None

    deployments.update_deployment(made["id"], {"overrides": {"family": "mine"}})

    assert deployments.get_deployment(made["id"])["version"].family == "mine"


def test_an_unrecognised_model_still_produces_a_usable_deployment():
    """The empty-catalog guarantee, one layer up: a local server's model that
    matches nothing must still be addable and callable."""
    conn = _connection("local", kind="local", key_required=False, secret=None)
    made = deployments.add_deployment(connection_id=conn["id"], model="some-gguf-thing")

    assert made["version"].family is None
    assert made["version"].capabilities.vision is Support.UNKNOWN
    assert made["version"].effort.kind is EffortKind.UNKNOWN
    assert deployments.is_ready(made) is True


# --- what a deployment no longer carries -----------------------------------

def test_the_authored_guesses_are_gone_rather_than_ported():
    """`tier.cost` and `tier.speed` are measurable, `tags` was derived from
    those guesses and read by nothing, and `billing` was a name regex that
    `kind` answers better. None of them survive."""
    conn = _connection("c")
    made = deployments.add_deployment(connection_id=conn["id"], model="claude-sonnet-5")

    for gone in ("tier", "tags", "billing", "caps"):
        assert gone not in made, f"{gone} should not exist on a deployment"


def test_the_one_authored_number_that_survives_lives_on_the_version():
    """Quality has no measurable proxy short of an evaluation harness, so it
    stays — as a catalog fact about the model, not a field on the pairing."""
    conn = _connection("c")
    made = deployments.add_deployment(connection_id=conn["id"], model="claude-opus-5")

    assert made["version"].quality == 5
    assert "quality" not in made


# --- editing and removing --------------------------------------------------

def test_a_patch_cannot_repoint_a_deployment_at_a_different_model():
    """It would carry this deployment's cooldown history and cost record across
    to something that has never been called."""
    conn = _connection("c")
    made = deployments.add_deployment(connection_id=conn["id"], model="original")

    patched = deployments.update_deployment(
        made["id"], {"model": "something-else", "connectionId": "elsewhere", "label": "renamed"})

    assert patched["model"] == "original"
    assert patched["connectionId"] == conn["id"]
    assert patched["label"] == "renamed"


def test_removing_a_connection_takes_its_deployments_with_it():
    conn = _connection("doomed")
    deployments.add_deployment(connection_id=conn["id"], model="a")
    deployments.add_deployment(connection_id=conn["id"], model="b")

    removed = deployments.delete_connection(conn["id"])

    assert removed == 2
    assert deployments.list_deployments() == []
    assert connections.get_connection(conn["id"]) is None


def test_a_deleted_deployment_leaves_no_history_for_its_id_to_inherit():
    """Ids are reused once freed, so a cooldown left behind would be handed to
    a model that has never been called."""
    conn = _connection("c")
    made = deployments.add_deployment(connection_id=conn["id"], model="flaky")
    availability.record(made["id"], "quota", detail="out of credit")
    assert availability.is_eligible(made["id"]) is False

    deployments.delete_deployment(made["id"])
    reborn = deployments.add_deployment(connection_id=conn["id"], model="flaky")

    assert reborn["id"] == made["id"], "the id is genuinely reused"
    assert availability.is_eligible(reborn["id"]) is True


# --- readiness -------------------------------------------------------------

def test_a_connection_that_needs_a_key_and_has_one_is_ready():
    conn = _connection("cloud")
    made = deployments.add_deployment(connection_id=conn["id"], model="m")

    assert deployments.is_ready(made) is True


def test_a_connection_that_needs_a_key_and_lacks_one_is_not():
    conn = _connection("cloud", secret=None)
    made = deployments.add_deployment(connection_id=conn["id"], model="m")

    assert deployments.is_ready(made) is False


def test_a_keyless_local_server_is_ready_with_no_secret_at_all():
    conn = _connection("local", kind="local", key_required=False, secret=None)
    made = deployments.add_deployment(connection_id=conn["id"], model="m")

    assert deployments.is_ready(made) is True


# --- adding several --------------------------------------------------------

def test_one_bad_name_among_several_does_not_lose_the_rest():
    conn = _connection("c")
    deployments.add_deployment(connection_id=conn["id"], model="already-there")

    result = deployments.add_deployments(
        conn["id"], ["fresh", "already-there", {"model": "with-label", "label": "Nice"}])

    assert [d["model"] for d in result["added"]] == ["fresh", "with-label"]
    assert [f["model"] for f in result["failed"]] == ["already-there"]


def test_a_deployment_is_addable_knowing_nothing_but_its_name():
    conn = _connection("c")
    result = deployments.add_deployments(conn["id"], ["just-a-name"])

    assert result["failed"] == []
    assert result["added"][0]["version"].model == "just-a-name"


# --- the effort scheme reaches the deployment ------------------------------

def test_a_deployment_knows_what_reasoning_its_model_offers():
    """The point of the catalog being hydrated in rather than stored: this was
    not recorded when the deployment was made, and did not need to be."""
    conn = _connection("c")
    made = deployments.add_deployment(connection_id=conn["id"], model="gpt-5.6-luna")

    scheme = made["version"].effort
    assert scheme.kind is EffortKind.TIERS
    assert scheme.supports(Effort.MAX) is True


def test_keying_on_the_model_would_bench_both_routes():
    """The alternative, demonstrated rather than described.

    Runs the same availability store under both keying choices against the same
    two deployments. Keying on the model is one character shorter at the call
    site and takes a working route offline; keying on the pairing does not. The
    difference is invisible in every other test in this file, which is why this
    one is written out.
    """
    direct = _connection("direct")
    gateway = _connection("gateway")
    a = deployments.add_deployment(connection_id=direct["id"], model="shared-model")
    b = deployments.add_deployment(connection_id=gateway["id"], model="shared-model")

    # The tempting simplification: the model is what failed, so bench the model.
    availability.record(b["model"], "quota", detail="the gateway is out of credit")
    assert availability.is_eligible(a["model"]) is False, (
        "this is the bug: the direct route, with its own key, is now benched too")

    availability.clear(b["model"])

    # What this build actually does: bench the route that failed.
    availability.record(b["id"], "quota", detail="the gateway is out of credit")
    assert availability.is_eligible(a["id"]) is True
    assert availability.is_eligible(b["id"]) is False


# --- the roster that already existed ----------------------------------------

def _legacy_models_file(*rows):
    """Write a `models.json` in the shape the flat registry stored."""
    from jarvis.store import write_json

    write_json("models", {"entries": list(rows)})


def test_an_existing_roster_is_carried_across_rather_than_lost(scratch):
    """The upgrade a real install goes through.

    An architecture that starts clean by emptying somebody's configured roster
    is a bug however tidy the result is. The user chose these models; that is
    their data, not this build's inference.
    """
    conn = _connection("cloud")
    _legacy_models_file(
        {"id": "kept", "connectionId": conn["id"], "model": "some-model",
         "label": "The good one", "enabled": True, "notes": "my favourite"},
        {"id": "off", "connectionId": conn["id"], "model": "other-model",
         "label": "other-model", "enabled": False, "notes": ""},
    )

    rows = deployments.list_deployments()

    assert [r["model"] for r in rows] == ["some-model", "other-model"]
    kept = rows[0]
    assert kept["label"] == "The good one"
    assert kept["notes"] == "my favourite"
    assert rows[1]["enabled"] is False, "a model switched off stays switched off"


def test_what_the_old_build_guessed_is_not_carried_across(scratch):
    """The other half, and the one that makes this a rebuild rather than a port.

    `caps`, `tier`, `tags` and `billing` were all produced by matching regular
    expressions against the model's name. Copying them would preserve the wrong
    answers past the point where a better one became available — this row's own
    `caps` claims the model cannot see, which is exactly the confident-boolean
    the catalog exists to stop.
    """
    conn = _connection("cloud")
    _legacy_models_file({
        "id": "old", "connectionId": conn["id"], "model": "some-model",
        "label": "some-model", "enabled": True,
        "caps": {"tools": True, "vision": False}, "tier": {"speed": 5, "quality": 2, "cost": 1},
        "tags": ["fast", "cheap"], "billing": "free",
    })

    [row] = deployments.list_deployments()

    for gone in ("caps", "tier", "tags", "billing"):
        assert gone not in row, f"{gone} was a guess and must not survive"
    assert row["version"].capabilities.vision is Support.UNKNOWN, (
        "an unasked capability reads as unknown, not as the old confident False")
    assert row["discovered"] == {} and row["overrides"] == {}


def test_a_label_nobody_actually_chose_does_not_come_across_as_one(scratch):
    """The old writer defaulted `label` to the model name, so a row nobody had
    named was indistinguishable from one named after itself — and renaming the
    model later left the old name behind as though somebody had picked it."""
    conn = _connection("cloud")
    _legacy_models_file({"id": "x", "connectionId": conn["id"], "model": "some-model",
                         "label": "some-model", "enabled": True})

    [row] = deployments.list_deployments()

    assert row["label"] is None


def test_the_old_file_is_archived_rather_than_deleted_or_left_in_place(scratch):
    """Renamed: it is the only record of what was configured before this, it
    costs a few kilobytes, and a rename is the one cleanup a person can undo.
    Left in place it would be re-adopted on every read."""
    from jarvis.store import data_file_path

    conn = _connection("cloud")
    _legacy_models_file({"id": "x", "connectionId": conn["id"], "model": "some-model",
                         "enabled": True})
    deployments.list_deployments()

    assert not data_file_path("models").exists()
    assert data_file_path("models.archived").exists()

    # And a deletion afterwards stays deleted rather than being undone by a
    # second adoption on the next read.
    deployments.delete_deployment(deployments.list_deployments()[0]["id"])
    assert deployments.list_deployments() == []


def test_a_fresh_install_gains_nothing_from_an_adoption_it_has_no_use_for(scratch):
    assert deployments.list_deployments() == []
    from jarvis.store import data_file_path
    assert not data_file_path("model-deployments").exists()


def test_what_a_provider_reported_at_add_time_is_not_dropped_on_the_way_in():
    """The rows arrive in the adapter's camelCase, the catalog reads snake_case.

    This had its own key list and looked for `context_tokens` while the add flow
    sends `contextTokens`, so the context window a provider had just told us
    about was silently discarded — nothing failed, the number simply was not
    there, and the router's "too small for this much text" check had nothing to
    read. Both sides now go through one translator.
    """
    conn = _connection("cloud")

    result = deployments.add_deployments(conn["id"], [
        {"model": "big-model", "contextTokens": 200_000},
        {"model": "described", "capabilities": ["vision", "tool_use"]},
        "just-a-name",
    ])

    assert result["failed"] == []
    by_model = {d["model"]: d for d in result["added"]}
    assert by_model["big-model"]["version"].context_tokens == 200_000
    assert by_model["described"]["version"].capabilities.vision is Support.YES
    assert by_model["described"]["version"].capabilities.tools is Support.YES
    assert by_model["just-a-name"]["version"].model == "just-a-name"


def test_a_label_the_picker_filled_in_is_not_recorded_as_one_somebody_chose():
    """The discovery flow defaults a row's label to its model id so the picker
    has something to show, and that default arrives here on every add."""
    conn = _connection("cloud")

    result = deployments.add_deployments(conn["id"], [
        {"model": "some-model", "label": "some-model"},
        {"model": "other-model", "label": "The good one"},
    ])

    by_model = {d["model"]: d for d in result["added"]}
    assert by_model["some-model"]["label"] is None
    assert by_model["other-model"]["label"] == "The good one"


def test_a_row_with_no_model_name_fails_alone_rather_than_losing_the_batch():
    conn = _connection("cloud")

    result = deployments.add_deployments(conn["id"], [{"label": "nameless"}, "real-model"])

    assert [d["model"] for d in result["added"]] == ["real-model"]
    assert len(result["failed"]) == 1


def test_a_model_cannot_take_an_id_the_api_already_uses_for_something_else():
    """`/api/models/<id>` shares its path space with the static segments beside
    it. A model called "Catalog" slugifies to `catalog`, and that deployment
    would then be impossible to edit or delete — `/api/models/catalog` answers
    with the browse route instead, and nothing reports a problem."""
    conn = _connection("cloud")

    one = deployments.add_deployment(connection_id=conn["id"], model="m1", label="Catalog")
    two = deployments.add_deployment(connection_id=conn["id"], model="m2", label="Recheck")

    assert one["id"] not in deployments.RESERVED_IDS
    assert two["id"] not in deployments.RESERVED_IDS
    assert deployments.get_deployment(one["id"]) is not None
