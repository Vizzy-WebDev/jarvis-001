"""Discovery: what a provider says it has, and what that means for the roster.

Two things here did not exist before. A model the provider had retired stayed
configured forever, failing every time it was tried and being benched on a
cooldown timer, so the same dead end was rediscovered every few hours for the
life of the install. And a gateway publishing which parameters a model accepts
was telling us whether reasoning could be asked for at all, which nothing read.

The reconciliation tests use the real HTTP stub where they can, because what is
being proved is that a changed listing changes the roster — and a stubbed
adapter would prove only that the test author wired the comparison up the way
they intended.
"""

from __future__ import annotations

import pytest

from jarvis.catalog import EffortKind, Lifecycle, Source, Support
from jarvis.gateway import availability, connections, deployments, discovery, effort
from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolated(scratch):
    availability.reset_for_tests()
    effort.reset_for_tests()
    yield
    availability.reset_for_tests()
    effort.reset_for_tests()


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    yield server
    server.stop()


def _connection(stub, **kw):
    defaults = dict(adapter="openai-compatible", base_url=stub.base_url, label="stub",
                    provider="custom", kind="local", key_required=False)
    defaults.update(kw)
    return connections.add_connection(**defaults)


# --- reading a listing -----------------------------------------------------

def test_a_listing_becomes_catalog_shaped_facts():
    """Adapters answer in their wire's vocabulary and in camelCase; the catalog
    speaks one vocabulary in snake_case. One translation, not three."""
    [row] = discovery.normalise([
        {"model": "a-model", "label": "A Model", "contextTokens": 128000}])

    assert row.model == "a-model"
    assert row.discovered["label"] == "A Model"
    assert row.discovered["context_tokens"] == 128000


def test_a_row_with_no_model_name_is_dropped_rather_than_stored_empty():
    assert discovery.normalise([{"label": "nameless"}, {"model": "  "}, None]) == []


def test_a_reported_capability_array_is_read_where_one_is_offered():
    [row] = discovery.normalise([
        {"model": "m", "capabilities": ["completion", "vision", "tool_use"]}])

    assert row.discovered["capabilities"] == {"vision": True, "tools": True}


def test_an_unrecognised_capability_name_is_dropped_not_guessed_at():
    [row] = discovery.normalise([{"model": "m", "capabilities": ["telepathy"]}])

    assert "capabilities" not in row.discovered


# --- what a parameter list settles -----------------------------------------

def test_a_parameter_list_naming_reasoning_establishes_that_it_can_be_asked():
    scheme = discovery.effort_from_parameters(["tools", "temperature", "reasoning"])

    assert scheme is not None
    assert scheme.kind is EffortKind.TIERS
    assert scheme.controllable is True


def test_a_parameter_list_NOT_naming_reasoning_settles_it_the_other_way():
    """The valuable case, and the reason to read the list at all.

    `NONE` here is a discovered fact, not an assumption — and the only way to
    learn it without spending a rejected request to find out.
    """
    scheme = discovery.effort_from_parameters(["tools", "temperature", "max_tokens"])

    assert scheme is not None
    assert scheme.kind is EffortKind.NONE
    assert scheme.controllable is False


def test_a_provider_that_reports_no_parameter_list_leaves_the_question_open():
    """Not reported is not the same as reported-and-absent. Returning a scheme
    here would turn silence into a claim."""
    assert discovery.effort_from_parameters(None) is None
    assert discovery.effort_from_parameters([]) is None


def test_a_discovered_scheme_beats_the_shipped_catalogs_guess():
    """Precedence, end to end: the provider's own answer about its own model
    outranks a pattern match."""
    [row] = discovery.normalise([
        {"model": "claude-sonnet-5", "supported_parameters": ["tools", "max_tokens"]}])

    from jarvis.catalog import resolve

    version = resolve(model="claude-sonnet-5", discovered=row.discovered)

    assert version.effort.kind is EffortKind.NONE, "discovery wins over the catalog"
    assert version.source_of("effort") is Source.DISCOVERED


# --- reconciliation --------------------------------------------------------

def test_a_model_that_stopped_being_listed_is_marked_retired(stub):
    """The behaviour that did not exist. A retired model used to stay in the
    roster forever, failing and being benched on a timer, rediscovering the same
    dead end every few hours."""
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="going-away")

    stub.models = [{"id": "something-else", "object": "model"}]
    result = discovery.refresh(conn["id"])

    assert result.retired == (made["id"],)
    assert deployments.get_deployment(made["id"])["version"].lifecycle is Lifecycle.RETIRED


def test_retirement_marks_rather_than_deletes(stub):
    """A model vanishing is usually retirement, but it is also what a lost key
    or a bad morning at the provider looks like. Marking is reversible."""
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="going-away")

    stub.models = []
    discovery.refresh(conn["id"])

    assert deployments.get_deployment(made["id"]) is not None
    assert made["id"] in {d["id"] for d in deployments.list_deployments()}


def test_a_model_that_comes_back_stops_being_retired(stub):
    """The marking is a record of the last listing, not a verdict."""
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="flaky-listing")

    stub.models = []
    discovery.refresh(conn["id"])
    assert deployments.get_deployment(made["id"])["version"].lifecycle is Lifecycle.RETIRED

    stub.models = [{"id": "flaky-listing", "object": "model"}]
    listed, _ = discovery.fetch(conn["id"])
    counts = discovery.apply(discovery.reconcile(conn["id"], listed))

    assert counts["restored"] == 1
    assert deployments.get_deployment(made["id"])["version"].lifecycle is Lifecycle.CURRENT


def test_retirement_survives_a_restart(stub):
    """It round-trips through JSON, so the enum has to survive being a string.

    A version of the catalog that only accepted the enum turned every restored
    "retired" back into "unknown" — forgetting, on restart, the exact thing
    retirement exists to remember.
    """
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="going-away")

    stub.models = []
    discovery.refresh(conn["id"])

    from jarvis import store

    store.reset_for_tests()

    assert deployments.get_deployment(made["id"])["version"].lifecycle is Lifecycle.RETIRED


def test_a_listed_model_that_is_not_configured_is_offered_not_added(stub):
    """Discovering that a gateway offers four hundred models is not consent to
    configure four hundred models."""
    conn = _connection(stub)
    stub.models = [{"id": "one", "object": "model"}, {"id": "two", "object": "model"}]

    result = discovery.refresh(conn["id"])

    assert {row.model for row in result.added} == {"one", "two"}
    assert deployments.list_deployments() == []


def test_reconciliation_decides_nothing_and_writes_nothing(stub):
    """Separate from `apply` so a caller can show what WOULD change first."""
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="going-away")

    result = discovery.reconcile(conn["id"], discovery.normalise([{"model": "other"}]))

    assert result.retired == (made["id"],)
    assert deployments.get_deployment(made["id"])["version"].lifecycle is not Lifecycle.RETIRED


def test_reconciliation_only_looks_at_the_connection_it_was_asked_about(stub):
    """A model configured on a different connection is not missing from this
    listing, it is somebody else's."""
    conn_a = _connection(stub, label="a")
    conn_b = _connection(stub, label="b")
    deployments.add_deployment(connection_id=conn_b["id"], model="lives-on-b")

    result = discovery.reconcile(conn_a["id"], [])

    assert result.retired == ()


# --- unreachable is not the same as empty ----------------------------------

def test_being_unable_to_reach_a_provider_is_not_the_same_as_it_having_nothing(stub):
    """Different problems, different fixes. A caller that cannot tell them
    apart gives the wrong advice — kept from the old discovery flow, which got
    this right."""
    conn = _connection(stub)
    stub.fails(500, "everything is on fire")

    listed, error = discovery.fetch(conn["id"])

    assert listed == []
    assert error is not None


def test_an_unreachable_provider_retires_nothing(stub):
    """The dangerous case. A failed listing looks exactly like a listing that
    contains none of your models, and acting on it would retire the roster."""
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="still-fine")
    stub.fails(500, "everything is on fire")

    result = discovery.refresh(conn["id"])

    assert result.reachable is False
    assert result.retired == ()
    assert deployments.get_deployment(made["id"])["version"].lifecycle is not Lifecycle.RETIRED


def test_a_connection_that_no_longer_exists_answers_rather_than_raising():
    listed, error = discovery.fetch("nothing-here")

    assert listed == []
    assert error is not None


# --- discovered facts reach the deployment ---------------------------------

def test_a_refresh_updates_the_facts_of_a_model_that_is_already_configured(stub):
    """The models a user actually calls are the ones worth refreshing.

    An earlier version of `Reconciliation` carried only ids for the still-listed
    half, so `apply` had nothing to write and silently discarded everything the
    provider had just reported about every model in active use. Caught because
    the first draft of this test had to call `record_discovery` by hand to make
    its own assertion pass.
    """
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="stub-model")
    assert deployments.get_deployment(made["id"])["version"].context_tokens is None

    stub.models = [{"id": "stub-model", "object": "model",
                    "context_length": 64000,
                    "supported_parameters": ["tools", "max_tokens"]}]
    discovery.refresh(conn["id"])

    version = deployments.get_deployment(made["id"])["version"]
    assert version.lifecycle is Lifecycle.CURRENT
    assert version.context_tokens == 64000, "the listing's facts must be written"
    assert version.effort.kind is EffortKind.NONE, "including what it settled about reasoning"


def test_a_discovered_fact_cannot_be_written_through_the_ordinary_patch(stub):
    """`discovered` is what a provider said and `overrides` is what a person
    said. One patch surface that could write either would let a screen quietly
    overwrite the first with the second."""
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="m",
                                      discovered={"context_tokens": 111})

    deployments.update_deployment(made["id"], {"discovered": {"context_tokens": 999}})

    assert deployments.get_deployment(made["id"])["version"].context_tokens == 111


def test_a_user_override_still_beats_a_freshly_discovered_fact(stub):
    """Precedence holds after a refresh, not just at add time."""
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="stub-model",
                                      overrides={"context_tokens": 42})
    stub.models = [{"id": "stub-model", "object": "model", "context_length": 64000}]

    discovery.refresh(conn["id"])

    version = deployments.get_deployment(made["id"])["version"]
    assert version.context_tokens == 42
    assert version.source_of("context_tokens") is Source.USER


def test_capabilities_reported_by_a_local_server_reach_the_version(stub):
    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="m")
    deployments.record_discovery(
        made["id"], discovery.normalise(
            [{"model": "m", "capabilities": ["vision"]}])[0].discovered)

    assert deployments.get_deployment(made["id"])["version"].capabilities.vision is Support.YES


def test_everything_a_listing_produces_survives_the_round_trip_to_disk(stub):
    """The invariant broken twice while building this layer.

    A deployment's `discovered` facts are stored as JSON. Putting a catalog
    object in them — a `Lifecycle` first, then an `EffortScheme` — typechecks,
    reads correctly in memory, and then either raises on write or silently comes
    back as something else on read. Both happened. This asserts the shape of the
    rule rather than the two instances of breaking it.
    """
    import json

    [row] = discovery.normalise([{
        "model": "everything", "label": "Everything",
        "contextTokens": 128000,
        "capabilities": ["vision", "tools"],
        "supported_parameters": ["tools", "reasoning"],
    }])

    json.dumps(row.discovered)  # must not raise

    conn = _connection(stub)
    made = deployments.add_deployment(connection_id=conn["id"], model="everything",
                                      discovered=row.discovered)
    from jarvis import store

    store.reset_for_tests()
    restored = deployments.get_deployment(made["id"])["version"]

    assert restored.context_tokens == 128000
    assert restored.capabilities.vision is Support.YES
    assert restored.lifecycle is Lifecycle.CURRENT
    assert restored.effort.kind is EffortKind.TIERS, "the scheme must survive being a file"
    assert restored.effort.controllable is True
