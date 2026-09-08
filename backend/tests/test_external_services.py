"""Keys for services that are not model providers.

The behaviour worth pinning: a key goes in and never comes back out, the
structure/secret split holds, and adding under an existing name is a refusal
rather than a silent overwrite — a real, confirmed bug in the original.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis import config, external_services


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def test_an_empty_install_answers_the_recorded_shape(client):
    assert client.get("/api/external-services").json() == {"services": []}


def test_adding_a_service_stores_the_key_and_reports_only_that_it_is_set(client):
    answer = client.post("/api/external-services",
                         json={"label": "Deepgram", "key": "dg-real-key-123"})
    assert answer.json()["service"] == {
        "ref": "deepgram", "label": "Deepgram", "configured": True,
        "extraFieldLabel": None, "extraFieldConfigured": False}
    assert "dg-real-key-123" not in answer.text
    assert "dg-real-key-123" not in client.get("/api/external-services").text
    # It really was stored, where every other secret in this app lives.
    assert config.get_secret("deepgram") == "dg-real-key-123"


def test_the_name_becomes_a_stable_ref(client):
    ref = client.post("/api/external-services",
                      json={"label": "Eleven Labs", "key": "k"}).json()["service"]["ref"]
    assert ref == "eleven-labs"


def test_a_name_that_produces_no_usable_id_is_refused(client):
    refused = client.post("/api/external-services", json={"label": "!!!", "key": "k"})
    assert refused.status_code == 400
    assert external_services.list_services() == []


def test_a_service_with_no_key_is_refused(client):
    assert client.post("/api/external-services", json={"label": "Deepgram"}).status_code == 400


def test_adding_over_an_existing_service_is_an_error_not_a_silent_overwrite(client):
    """The confirmed bug this exists for: typing an already-connected service's
    name into the ADD form replaced its key with nothing shown."""
    client.post("/api/external-services", json={"label": "Deepgram", "key": "first"})
    collision = client.post("/api/external-services", json={"label": "Deepgram", "key": "second"})

    assert collision.status_code == 400
    assert "already connected" in collision.json()["error"]
    assert config.get_secret("deepgram") == "first", "the working key was overwritten anyway"


def test_a_rows_own_save_DOES_replace_the_key(client):
    """The other half of the same rule: rotating a key through the row that owns
    it is exactly what that control is for."""
    client.post("/api/external-services", json={"label": "Deepgram", "key": "first"})
    updated = client.post("/api/external-services/deepgram", json={"key": "second"})
    assert updated.json()["service"]["configured"] is True
    assert config.get_secret("deepgram") == "second"


def test_a_second_field_is_stored_beside_the_key_and_cleared_with_it(client):
    client.post("/api/external-services", json={
        "label": "ElevenLabs", "key": "k", "extraFieldLabel": "Voice ID",
        "extraFieldValue": "voice-42"})
    [row] = client.get("/api/external-services").json()["services"]
    assert row["extraFieldLabel"] == "Voice ID" and row["extraFieldConfigured"] is True
    assert external_services.get_extra_field("elevenlabs") == "voice-42"

    # The row no longer declares a second field: nothing orphaned is left under it.
    client.post("/api/external-services/elevenlabs", json={"key": "k"})
    assert external_services.get_extra_field("elevenlabs") is None


def test_clearing_a_key_keeps_the_row_and_deleting_removes_it(client):
    client.post("/api/external-services", json={"label": "Deepgram", "key": "k"})

    assert client.delete("/api/external-services/deepgram").json() == {"ok": True}
    [row] = client.get("/api/external-services").json()["services"]
    assert row["configured"] is False, "the row should survive, ready for a new key"

    assert client.delete("/api/external-services/deepgram/full").json() == {"ok": True}
    assert client.get("/api/external-services").json() == {"services": []}


def test_an_unknown_service_is_a_clean_404(client):
    assert client.post("/api/external-services/nope", json={"key": "k"}).status_code == 404
    assert client.delete("/api/external-services/nope").status_code == 404
    assert client.post("/api/external-services/nope/test").status_code == 404


def test_with_no_tester_the_answer_is_could_not_check_not_it_works(client):
    """A green tick nobody earned is worse than no tick."""
    client.post("/api/external-services", json={"label": "Deepgram", "key": "k"})
    answer = client.post("/api/external-services/deepgram/test")
    assert answer.status_code == 501
    assert answer.json()["ok"] is False


def test_a_key_saved_before_this_store_existed_still_appears(client):
    """The migration case: the version this replaces saved a real Deepgram key
    with no row to go with it. Without this the key keeps working but vanishes
    from the list until someone re-adds it under exactly the same name."""
    config.save_secret("deepgram", "from-the-old-world")
    [row] = client.get("/api/external-services").json()["services"]
    assert row["label"] == "Deepgram" and row["configured"] is True
