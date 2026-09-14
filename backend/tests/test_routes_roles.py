"""Which model does which job, over HTTP.

The behaviour worth pinning here is what the route does NOT do. An assignment
is a preference: naming a deployment that was deleted, or one that is switched
off, or one that is rate-limited right now, is not an error — the router
honours a slot by moving it to the front of the ranking, never by removing
everything else, so a stale assignment degrades to ordinary ranking. A route
that refused those would be stricter than the router and would turn the pin
back into the single point of failure the design avoids.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.catalog import Effort
from jarvis.gateway import connections, deployments, slots
from jarvis.gateway.slots import Role


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    slots.reset_for_tests()
    yield TestClient(create_app())
    slots.reset_for_tests()


@pytest.fixture
def roster(client):
    conn = connections.add_connection(
        adapter="openai-compatible", base_url="https://api.example", label="cloud",
        provider="custom", kind="gateway", key_required=True, secret="sk-not-a-real-key-000")
    deployments.add_deployment(connection_id=conn["id"], model="gpt-5-mini", label="Quick")
    deployments.add_deployment(connection_id=conn["id"], model="some-local-model")
    return client


def _role(client, role_id):
    return next(r for r in client.get("/api/roles").json()["roles"] if r["id"] == role_id)


def test_every_job_is_listed_whether_set_or_not(roster):
    """The unassigned ones are the ones a person most needs to see: a list that
    showed only what had been configured would hide the whole feature from
    anyone who has never used it."""
    body = roster.get("/api/roles").json()

    assert {r["id"] for r in body["roles"]} == {r.value for r in Role}
    assert all(r["assigned"] is False for r in body["roles"])
    assert all(r["label"] and r["description"] for r in body["roles"])


def test_a_role_row_carries_exactly_these_keys(roster):
    from jarvis.routes.roles import ROLE_KEYS

    assert set(roster.get("/api/roles").json()["roles"][0]) == set(ROLE_KEYS)


def test_setting_a_model_reports_back_what_it_resolved_to(roster):
    [quick] = [d for d in deployments.list_deployments() if d["model"] == "gpt-5-mini"]

    answer = roster.put("/api/roles/voice", json={"deploymentId": quick["id"]}).json()

    assert answer["ok"] is True
    assert answer["role"]["deploymentId"] == quick["id"]
    assert answer["role"]["deployment"]["label"] == "Quick"
    assert answer["role"]["assigned"] is True
    assert slots.pin_for(Role.VOICE) == quick["id"]


def test_the_levels_offered_are_the_ones_that_model_actually_takes(roster):
    """Not a fixed ladder. The OpenAI-shaped wire accepts a level above `high`
    and Gemini's enum does not, so a picker showing one list for every model
    would be offering a setting that silently clamps."""
    [quick] = [d for d in deployments.list_deployments() if d["model"] == "gpt-5-mini"]
    roster.put("/api/roles/voice", json={"deploymentId": quick["id"]})

    choices = [c["id"] for c in _role(roster, "voice")["effortChoices"]]

    assert choices == ["OFF", "MINIMAL", "LOW", "MEDIUM", "HIGH", "MAX"]


def test_a_model_with_no_reasoning_control_offers_no_levels(roster):
    """Empty is a real answer — this model has none, or nobody has established
    that it has any. A screen renders that as "not available", not as an empty
    dropdown that looks broken."""
    [local] = [d for d in deployments.list_deployments() if d["model"] == "some-local-model"]
    roster.put("/api/roles/background", json={"deploymentId": local["id"]})

    assert _role(roster, "background")["effortChoices"] == []


def test_an_effort_can_be_set_without_choosing_a_model(roster):
    """The more useful half for anyone without a favourite model: "whatever
    gets picked, ask it to think this hard"."""
    answer = roster.put("/api/roles/control", json={"effort": "HIGH"}).json()

    assert answer["role"]["effort"] == "HIGH"
    assert answer["role"]["deploymentId"] is None
    assert slots.effort_for(Role.CONTROL) is Effort.HIGH


def test_the_two_halves_clear_independently(roster):
    [quick] = [d for d in deployments.list_deployments() if d["model"] == "gpt-5-mini"]
    roster.put("/api/roles/voice", json={"deploymentId": quick["id"], "effort": "LOW"})

    roster.put("/api/roles/voice", json={"deploymentId": None})
    assert _role(roster, "voice")["deploymentId"] is None
    assert _role(roster, "voice")["effort"] == "LOW", "the effort half survives"

    roster.put("/api/roles/voice", json={"effort": None})
    assert _role(roster, "voice")["assigned"] is False


def test_clearing_a_role_hands_it_back_to_ranking(roster):
    [quick] = [d for d in deployments.list_deployments() if d["model"] == "gpt-5-mini"]
    roster.put("/api/roles/voice", json={"deploymentId": quick["id"], "effort": "LOW"})

    assert roster.delete("/api/roles/voice").json()["role"]["assigned"] is False
    assert slots.get(Role.VOICE).assigned is False


def test_a_pin_at_something_that_no_longer_exists_is_reported_not_refused(roster):
    """A stale preference is not an error — it degrades to ordinary ranking.
    The id is still reported so a screen can say what it was, rather than
    pretending nothing had ever been set."""
    answer = roster.put("/api/roles/conversation", json={"deploymentId": "deleted-long-ago"})

    assert answer.status_code == 200
    role = answer.json()["role"]
    assert role["deploymentId"] == "deleted-long-ago"
    assert role["deployment"] is None
    assert role["effortChoices"] == []


def test_a_pin_at_a_switched_off_model_is_accepted_and_says_so(roster):
    """Refusing it here would be stricter than the router, which offers the
    rest of the roster behind the pin rather than failing the turn."""
    [local] = [d for d in deployments.list_deployments() if d["model"] == "some-local-model"]
    deployments.update_deployment(local["id"], {"enabled": False})

    role = roster.put("/api/roles/utility", json={"deploymentId": local["id"]}).json()["role"]

    assert role["deployment"]["enabled"] is False


def test_a_job_nobody_ships_is_a_clean_404(roster):
    assert roster.put("/api/roles/telepathy", json={"effort": "LOW"}).status_code == 404
    assert roster.delete("/api/roles/telepathy").status_code == 404


def test_a_level_nobody_ships_is_refused_rather_than_stored(roster):
    assert roster.put("/api/roles/voice", json={"effort": "ludicrous"}).status_code == 400
    assert slots.effort_for(Role.VOICE) is None


def test_no_secret_reaches_the_roles_route(roster):
    [quick] = [d for d in deployments.list_deployments() if d["model"] == "gpt-5-mini"]
    roster.put("/api/roles/voice", json={"deploymentId": quick["id"]})

    raw = roster.get("/api/roles").text
    assert "sk-not-a-real-key-000" not in raw and "secretRef" not in raw
