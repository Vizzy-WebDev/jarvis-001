"""The voice seams: which provider speaks, and which recognition runs.

The rule underneath all of it is that nothing is matched by a name this code
knows in advance. A service's ref is whatever the user typed, slugified, so a
provider is found by ASKING each adapter, and each adapter recognises itself
typo-tolerantly — a plain substring check failed live, twice, on two real typos.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis import external_services, stt, tts
from jarvis.tts import elevenlabs, generic


@pytest.fixture(autouse=True)
def _isolated(scratch):
    elevenlabs.reset_for_tests()
    yield
    elevenlabs.reset_for_tests()


@pytest.fixture
def client():
    from jarvis.main import create_app

    return TestClient(create_app())


# --- recognising a provider from whatever the user typed ----------------------

@pytest.mark.parametrize("typed", ["elevenlabs", "ElevenLabs", "eleven-labs", "11labs",
                                   "elevenlab", "elevenlap", "elevnlabs"])
def test_a_provider_recognises_itself_through_a_typo(typed):
    """"elevenlap" is the one that matters: a b→p slip does not contain
    "elevenlab" as a substring AT ALL, so the substring check that came before
    this matched nothing — and the service silently vanished from both the voice
    picker and the Test button, with no error, because nothing was wrong from
    either side's point of view."""
    assert elevenlabs.matches_ref(typed) is True


@pytest.mark.parametrize("typed", ["deepgram", "openai", "fish-audio", "el", "notion", ""])
def test_something_else_is_not_mistaken_for_it(typed):
    assert elevenlabs.matches_ref(typed) is False


def test_a_short_ref_is_never_fuzzy_matched():
    """Two or three characters cannot be matched by edit distance without real
    risk of claiming someone else's service."""
    assert elevenlabs.matches_ref("11l") is False


def test_the_data_driven_registry_recognises_its_own_names():
    assert generic.matches_ref("fish audio") is True
    assert generic.matches_ref("fishaudo") is True
    assert generic.matches_ref("elevenlabs") is False


# --- what the picker offers ---------------------------------------------------

def test_only_configured_services_a_real_adapter_recognises_are_offered(client):
    external_services.add_or_update(label="ElevenLabs", key="k")
    external_services.add_or_update(label="Deepgram", key="k")        # not a voice
    external_services.add_or_update(label="Some Other Thing", key="k")  # nothing knows it

    offered = client.get("/api/tts/providers").json()["providers"]
    assert [p["label"] for p in offered] == ["ElevenLabs"]
    assert offered[0]["configured"] is True


def test_the_browsers_own_voice_is_never_listed_here(client):
    """It has no server side at all — the browser talks to nothing. The client
    always offers it, which is what makes it the one voice always available."""
    external_services.add_or_update(label="ElevenLabs", key="k")
    offered = client.get("/api/tts/providers").json()["providers"]
    assert not any("browser" in str(p["id"]).lower() for p in offered)


def test_several_services_can_match_the_same_adapter(client):
    """The data-driven registry legitimately matches more than one configured
    service at once, each with its own endpoint — taking only the first match
    per adapter would quietly drop the rest."""
    external_services.add_or_update(label="Fish Audio", key="k")
    external_services.add_or_update(label="Fish", key="k2")
    assert len(client.get("/api/tts/providers").json()["providers"]) == 2


# --- speaking -----------------------------------------------------------------

def test_asking_for_speech_with_nothing_configured_says_what_is_missing(client):
    answer = client.post("/api/tts", json={"text": "hello"})
    assert answer.status_code == 400
    assert answer.json()["code"] == "NO_API_KEY"


def test_empty_text_is_refused_before_any_provider_is_asked(client):
    assert client.post("/api/tts", json={"text": "   "}).status_code == 400


def test_one_call_can_override_the_saved_default(client, monkeypatch):
    """A configured provider is not automatically THE provider: the saved
    preference decides, and a single call can name a different one without
    changing it. Matches the original — and it is why a service can be
    connected and still silent until a voice is chosen."""
    external_services.add_or_update(label="ElevenLabs", key="k")
    monkeypatch.setattr(elevenlabs, "stream",
                        lambda text, voice=None, ref=None: iter(
                            [{"buffer": b"AUDIO", "mimeType": "audio/mpeg"}]))

    assert client.post("/api/tts", json={"text": "hi"}).status_code == 400
    named = client.post("/api/tts", json={"text": "hi", "provider": "elevenlabs"})
    assert named.status_code == 200 and named.content == b"AUDIO"


def test_speech_is_recorded_as_usage_the_moment_it_is_attempted(client, monkeypatch):
    """Most providers bill per character REQUESTED, not per chunk our own
    playback happens to consume."""
    from jarvis.cost import store as cost_store

    from jarvis.prefs import set_prefs

    external_services.add_or_update(label="ElevenLabs", key="k")
    set_prefs({"ttsProvider": "elevenlabs"})
    monkeypatch.setattr(elevenlabs, "stream",
                        lambda text, voice=None, ref=None: iter(
                            [{"buffer": b"AUDIO", "mimeType": "audio/mpeg"}]))

    recorded: list[dict] = []
    monkeypatch.setattr(cost_store, "record_event",
                        lambda **kw: recorded.append(kw) or 1)

    answer = client.post("/api/tts", json={"text": "hello there"})
    assert answer.status_code == 200 and answer.content == b"AUDIO"
    assert recorded[0]["unit_kind"] == "characters"
    assert recorded[0]["units_out"] == len("hello there")


def test_bookkeeping_failing_never_stops_the_voice(client, monkeypatch):
    from jarvis.cost import store as cost_store

    from jarvis.prefs import set_prefs

    external_services.add_or_update(label="ElevenLabs", key="k")
    set_prefs({"ttsProvider": "elevenlabs"})
    monkeypatch.setattr(elevenlabs, "stream",
                        lambda text, voice=None, ref=None: iter(
                            [{"buffer": b"AUDIO", "mimeType": "audio/mpeg"}]))

    def boom(**_kw):
        raise RuntimeError("the ledger is on fire")

    monkeypatch.setattr(cost_store, "record_event", boom)
    assert client.post("/api/tts", json={"text": "hello"}).status_code == 200


# --- recognition --------------------------------------------------------------

def test_with_no_key_the_browsers_own_recognition_is_what_runs(client):
    assert client.get("/api/stt/status").json() == {"configured": False}
    assert stt.mode() == "browser"


def test_with_a_key_the_server_proxied_provider_is_live(client):
    external_services.add_or_update(label="Deepgram", key="dg-key")
    assert client.get("/api/stt/status").json() == {"configured": True}
    assert stt.mode() == "deepgram"


# --- the live key test dispatches for real now --------------------------------

def test_a_voice_providers_key_is_tested_through_its_own_adapter(client, monkeypatch):
    external_services.add_or_update(label="Elevenlab", key="saved-key")
    seen: list[str] = []
    monkeypatch.setattr(elevenlabs, "test_key",
                        lambda key, ref=None: seen.append(key) or {"ok": True})

    # The ref here is the TYPO the user typed — dispatch has to find the adapter
    # through the same recognition the picker uses.
    assert client.post("/api/external-services/elevenlab/test").json() == {"ok": True}
    assert seen == ["saved-key"], "the saved key was not the one tested"


def test_a_key_can_be_tested_before_it_is_saved(client, monkeypatch):
    external_services.add_or_update(label="ElevenLabs", key="old-key")
    seen: list[str] = []
    monkeypatch.setattr(elevenlabs, "test_key",
                        lambda key, ref=None: seen.append(key) or {"ok": True})

    client.post("/api/external-services/elevenlabs/test", json={"key": "brand-new-key"})
    assert seen == ["brand-new-key"]


def test_a_service_nothing_recognises_still_gets_an_honest_501(client):
    external_services.add_or_update(label="Some Other Thing", key="k")
    answer = client.post("/api/external-services/some-other-thing/test")
    assert answer.status_code == 501


def test_a_provider_that_cannot_be_reached_is_an_answer_not_a_crash(client, monkeypatch):
    external_services.add_or_update(label="ElevenLabs", key="k")

    def boom(key, ref=None):
        raise RuntimeError("the network is gone")

    monkeypatch.setattr(elevenlabs, "test_key", boom)
    answer = client.post("/api/external-services/elevenlabs/test")
    assert answer.status_code == 200 and answer.json()["ok"] is False
