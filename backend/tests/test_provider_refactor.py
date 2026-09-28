"""The model/provider layer's architecture fixes, one section per priority.

Every test here drives the real modules; where the wire matters it goes through a real
stub provider on a real socket (`stub_provider_server`), so what is asserted is what the
provider actually sent and what Jarvis actually did with it. Names are `test_pNN_...`
so each priority's tests can be run on their own (`-k p04`).

The adapter conformance suite (Priority 10) is in `tests/conformance/`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvis import conversation
from jarvis.db import get_db
from jarvis.models import auto, gateways, health, runtime, selection, store
from jarvis.models.errors import ProviderError
from jarvis.models.providers import _wire, anthropic_messages, gemini_generate, openai_chat, openai_responses
from jarvis.models.request import (REASONING_LEVELS, ChatRequest, GenerationOptions, ImagePart, MediaPart,
                                   TextPart, ToolCallPart, ToolResultPart)
from jarvis.models.types import RawError, Target
from test_models import (add, choose_auto, client, posted_models, run_step, select, serve,  # noqa: F401
                         two_connections)

MODELS = Path(__file__).resolve().parent.parent / "jarvis" / "models"


def connection(cid: str) -> store.Connection:
    found = store.get_connection(cid)
    assert found is not None
    return found


def holds_on(cid: str) -> list[store.Hold]:
    return [h for h in store.list_holds() if h.connection_id == cid]


def raw(status, body=None, headers=None, words="") -> RawError:
    return RawError(status=status, headers=headers or {}, body=body,
                    words=words or _wire.words_of(body), url="https://provider.example/v1")


def outcome_rows(cid: str) -> int:
    return get_db().execute("SELECT COUNT(*) FROM model_outcomes WHERE provider_id = ?", (cid,)).fetchone()[0]


# ================================================================================
# P01 — the ADAPTER sets an explicit scope, from the provider's own error body
# ================================================================================

def test_p01_a_provider_error_carries_every_field_and_refuses_an_unknown_scope():
    err = ProviderError("x", kind="rate", scope="model", retryable_elsewhere=True, retry_after_s=3.0, status=429)
    assert (err.kind, err.scope, err.retryable_elsewhere, err.retry_after_s, err.status) == (
        "rate", "model", True, 3.0, 429)
    assert ProviderError("y").scope == "unknown"  # nobody said: treated like one model, never wider
    with pytest.raises(ValueError):
        ProviderError("z", scope="connection-ish")


def test_p01_openai_reads_error_code_not_just_the_status():
    quota = openai_responses.normalize_error(raw(429, {"error": {"code": "insufficient_quota", "message": "q"}}))
    assert (quota.kind, quota.scope, quota.retryable_elsewhere) == ("billing", "provider", False)
    limited = openai_responses.normalize_error(raw(429, {"error": {"code": "rate_limit_exceeded", "message": "r"}}))
    assert (limited.kind, limited.scope, limited.retryable_elsewhere) == ("rate", "model", True)
    key = openai_responses.normalize_error(raw(401, {"error": {"code": "invalid_api_key", "message": "k"}}))
    assert (key.kind, key.scope) == ("auth", "credential")


def test_p01_the_same_status_means_different_things_by_body_on_a_chat_server():
    """429 is not one thing: pace is about one model, money is about the whole account."""
    pace = openai_chat.normalize_error(raw(429, {"error": {"message": "Rate limit exceeded, slow down"}}))
    money = openai_chat.normalize_error(raw(429, {"error": {"message": "You exceeded your quota; add credits"}}))
    assert (pace.kind, pace.scope) == ("rate", "model")
    assert (money.kind, money.scope) == ("billing", "credential")  # never model scope when it says quota/credit


def test_p01_an_unparseable_5xx_is_the_whole_provider_and_a_structured_one_is_the_model():
    html = openai_chat.normalize_error(raw(502, None, words="<html>Bad Gateway</html>"))
    structured = openai_chat.normalize_error(raw(502, {"error": {"code": 502, "message": "Provider returned error"}}))
    assert (html.kind, html.scope, html.retryable_elsewhere) == ("server", "provider", True)
    assert (structured.kind, structured.scope, structured.retryable_elsewhere) == ("server", "model", True)


def test_p01_529_means_overloaded_only_to_the_anthropic_adapter():
    assert anthropic_messages.normalize_error(raw(529, None)).kind == "overloaded"
    assert anthropic_messages.normalize_error(raw(529, None)).scope == "provider"
    assert openai_chat.normalize_error(raw(529, None)).kind == "server"  # just a 5xx to anyone else
    literal = re.compile(r"\b529\b")
    offenders = [p.name for p in MODELS.rglob("*.py") if literal.search(p.read_text(encoding="utf-8"))
                 and p.name != "anthropic_messages.py"]
    assert offenders == []


def test_p01_a_key_refused_on_the_wire_leaves_the_whole_connection_but_the_adapter_decided_it(client, serve):
    """End to end: Gemini answers a bad key with 400 INVALID_ARGUMENT plus ErrorInfo
    API_KEY_INVALID. A status-only reading would call that a bad request; Gemini's
    adapter reads the body and says credential — so Auto leaves every Gemini model."""
    bad_key = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                         "status": "INVALID_ARGUMENT",
                         "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                      "reason": "API_KEY_INVALID"}]}}
    gemini = serve("gemini-generatecontent", models=[{"id": "g-one"}, {"id": "g-two"}], fail_with=(400, bad_key, {}))
    fine = serve("openai-chat", models=[{"id": "fine"}])
    gid = add(client, gemini, label="Google")["connection"]["id"]
    add(client, fine, label="Fine")
    choose_auto(client)
    events, error = run_step()
    assert error is None and posted_models(fine) == ["fine"]
    assert len(gemini.posts()) == 1  # not g-two: the whole credential was refused
    assert [h.scope for h in holds_on(gid)] == ["credential"]


# ================================================================================
# P02 — the provider's own wait hint is read and used
# ================================================================================

def test_p02_retry_after_is_read_as_seconds_or_as_an_http_date():
    assert _wire.retry_after_s({"Retry-After": "30"}) == 30.0
    assert _wire.retry_after_s({"retry-after-ms": "1500"}) == 1.5
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    assert _wire.retry_after_s({"retry-after": "Mon, 28 Sep 2026 12:01:30 GMT"}, now=now) == 90.0
    assert _wire.retry_after_s({"retry-after": "soon"}) is None
    assert _wire.retry_after_s({}) is None


def test_p02_openai_reset_headers_use_the_limit_that_ran_out():
    headers = {"x-ratelimit-reset-requests": "6m0s", "x-ratelimit-reset-tokens": "20ms",
               "x-ratelimit-remaining-requests": "0", "x-ratelimit-remaining-tokens": "900"}
    assert _wire.reset_headers_s(headers) == 360.0  # requests ran out, so ITS reset
    err = openai_responses.normalize_error(raw(429, {"error": {"code": "rate_limit_exceeded"}}, headers))
    assert err.retry_after_s == 360.0
    assert _wire.duration_s("1.5s") == 1.5 and _wire.duration_s("1h2m3s") == 3723.0 and _wire.duration_s("x") is None


def test_p02_gemini_retrydelay_in_the_body_and_anthropic_retry_after_header_are_read():
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
                      "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}}
    assert gemini_generate.normalize_error(raw(429, body)).retry_after_s == 37.0
    anthropic = anthropic_messages.normalize_error(
        raw(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}, {"retry-after": "12"}))
    assert (anthropic.kind, anthropic.retry_after_s) == ("rate", 12.0)


def test_p02_a_retry_after_on_the_wire_sets_how_long_the_model_is_held(client, serve):
    limited = serve("openai-chat", models=[{"id": "busy"}],
                    fail_with=(429, {"error": {"message": "Rate limit reached"}}, {"Retry-After": "45"}))
    fine = serve("openai-chat", models=[{"id": "fine"}])
    cid = add(client, limited, label="Limited")["connection"]["id"]
    add(client, fine, label="Fine")
    choose_auto(client)
    before = datetime.now(timezone.utc)
    events, error = run_step()
    assert error is None and posted_models(fine) == ["fine"]
    [hold] = holds_on(cid)
    lasts = datetime.fromisoformat(hold.until.replace("Z", "+00:00")) - before
    assert hold.retry_after_s == 45.0 and timedelta(seconds=40) < lasts < timedelta(seconds=50)  # not 5 minutes


def test_p02_with_no_hint_the_hold_falls_back_to_the_exponential_backoff(client, serve):
    stub = serve("openai-chat", models=[{"id": "m"}])
    cid = add(client, stub)["connection"]["id"]
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    err = ProviderError("gone", kind="model", scope="model", status=404)
    first = health.record(connection(cid), "m", err, now=now)
    second = health.record(connection(cid), "m", err, now=now)
    assert first.until == "2026-09-28T12:05:00Z" and second.until == "2026-09-28T12:10:00Z"
    hinted = health.record(connection(cid), "m", ProviderError("slow", kind="rate", scope="model",
                                                                retry_after_s=7, status=429), now=now)
    assert hinted.until == "2026-09-28T12:00:07Z"  # the provider's own number wins


def test_p02_a_short_stated_wait_is_honoured_and_a_long_one_is_not_sat_through(client, serve, monkeypatch):
    from jarvis.models import attempt

    slept: list[float] = []
    monkeypatch.setattr(attempt, "_sleep", slept.append)
    short = serve("openai-chat", models=[{"id": "m"}],
                  fail_with=(503, {"error": {"message": "busy"}}, {"Retry-After": "2"}))
    cid, model = add(client, short)["connection"]["id"], "m"
    select(client, cid, model)
    run_step()
    # The fixture zeroes the default pauses, so the provider's stated 2s is what's waited, twice.
    assert slept == [2.0, 2.0] and len(short.posts()) == 3
    health.clear_on_success(connection(cid), model)

    slept.clear()
    short.fail_with = (503, {"error": {"message": "busy"}}, {"Retry-After": "60"})
    events, error = run_step()
    assert slept == [] and len(short.posts()) == 4  # asked once: a minute's wait is not sat through
    assert error is not None and "problem on its end" in str(error)


# ================================================================================
# P03 — cooldowns are keyed by credential, not just by connection
# ================================================================================

def test_p03_a_connection_names_its_credential_and_it_defaults_to_its_own_id(client, serve):
    cid = add(client, serve("openai-chat"))["connection"]["id"]
    assert connection(cid).credential_id == cid
    get_db().execute("UPDATE model_providers SET credential_id = 'shared-key' WHERE id = ?", (cid,))
    assert connection(cid).credential_id == "shared-key"


def test_p03_each_scope_is_held_under_its_own_key(client, serve):
    cid = add(client, serve("openai-chat"))["connection"]["id"]
    get_db().execute("UPDATE model_providers SET credential_id = 'cred-A' WHERE id = ?", (cid,))
    c = connection(cid)
    for scope in ("model", "credential", "provider"):
        health.record(c, "m1", ProviderError("x", kind="server", scope=scope))
    keys = {h.scope: h.hold_key for h in holds_on(cid)}
    assert keys == {"model": "cred-A\x1fm1", "credential": "cred-A", "provider": cid}


def test_p03_a_credential_failure_holds_every_connection_on_that_credential_and_no_other(client, serve):
    a = add(client, serve("openai-chat", models=[{"id": "a1"}]), label="A")["connection"]["id"]
    b = add(client, serve("openai-chat", models=[{"id": "b1"}]), label="B")["connection"]["id"]
    c = add(client, serve("openai-chat", models=[{"id": "c1"}]), label="C")["connection"]["id"]
    get_db().execute("UPDATE model_providers SET credential_id = 'one-key' WHERE id IN (?, ?)", (a, b))
    health.record(connection(a), "a1", ProviderError("refused", kind="auth", scope="credential", status=401))
    choose_auto(client)
    assert [x.connection.label for x in auto.candidates()] == ["C", "A", "B"]  # both on that key wait


def test_p03_a_model_failure_on_one_credential_leaves_the_same_model_on_another_alone(client, serve):
    a = add(client, serve("openai-chat", models=[{"id": "same"}]), label="A")["connection"]["id"]
    b = add(client, serve("openai-chat", models=[{"id": "same"}]), label="B")["connection"]["id"]
    health.record(connection(a), "same", ProviderError("down", kind="model", scope="model", status=404))
    holds = health.Holds.load()
    assert holds.blocking(connection(a), store.get_model(a, "same")) is not None
    assert holds.blocking(connection(b), store.get_model(b, "same")) is None


# ================================================================================
# P04 — a named model respects health, and is never swapped
# ================================================================================

def test_p04_a_named_model_under_a_provider_stated_wait_is_refused_and_not_called(client, serve):
    one, two, first_id, _ = two_connections(client, serve)
    select(client, first_id, "one-a")
    health.record(connection(first_id), "one-a",
                  ProviderError("slow down", kind="rate", scope="model", retry_after_s=40, status=429))
    events, error = run_step()
    assert error is not None and events == []
    assert re.match(r"That model failed recently and is cooling down\. Next attempt in (39|40)s\. "
                    r"Or switch to Auto\.", str(error))
    assert error.detail["reason"] == "cooling_down" and error.detail["retryInS"] in (39, 40)
    assert one.posts() == [] and two.posts() == []  # not tried, and nothing tried in its place


def test_p04_a_refused_key_holds_a_named_model_until_the_person_changes_the_key(client, serve):
    stub = serve("openai-chat", key="right", models=[{"id": "m"}])
    cid = add(client, stub, key="right")["connection"]["id"]
    select(client, cid, "m")
    stub.key = "rotated"
    events, error = run_step()
    assert error is not None and "didn't accept the key" in str(error)
    events, error = run_step()
    assert "cooling down" in str(error) and len(stub.posts()) == 1  # the 2nd turn never reached it
    client.patch(f"/api/models/{cid}", json={"apiKey": "rotated"})  # the person fixes it
    events, error = run_step()
    assert error is None and len(stub.posts()) == 2


def test_p04_an_ordinary_failure_does_not_lock_the_person_out_of_their_model(client, serve):
    stub = serve("openai-chat", models=[{"id": "m"}])
    cid = add(client, stub)["connection"]["id"]
    select(client, cid, "m")
    health.record(connection(cid), "m", ProviderError("hiccup", kind="server", scope="model", status=500))
    assert health.Holds.load().blocking(connection(cid), store.get_model(cid, "m")) is not None  # Auto would wait
    events, error = run_step()
    assert error is None and len(stub.posts()) == 1  # but the person's own choice is still tried
    assert holds_on(cid) == []  # and its answer lifted the hold


def test_p04_a_passing_connection_test_lifts_a_connection_wide_hold(client, serve):
    stub = serve("openai-chat", models=[{"id": "m"}])
    cid = add(client, stub)["connection"]["id"]
    select(client, cid, "m")
    health.record(connection(cid), "m", ProviderError("unreachable", kind="unreachable", scope="provider"))
    assert "cooling down" in str(run_step()[1])
    assert client.post(f"/api/models/{cid}/test").json()["ok"] is True
    events, error = run_step()
    assert error is None and len(stub.posts()) == 1


# ================================================================================
# P05 — catalog, policy and runtime kept apart
# ================================================================================

def test_p05_a_refresh_writes_the_catalog_and_never_the_policy(client, serve):
    stub = serve("openai-chat", models=[{"id": "a"}, {"id": "b"}])
    cid = add(client, stub)["connection"]["id"]
    client.delete(f"/api/models/{cid}/models/a")
    policy_before = get_db().execute("SELECT * FROM provider_policy").fetchall()
    stub.models = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert client.post(f"/api/models/{cid}/discover").json()["added"] == 1
    assert [tuple(r) for r in get_db().execute("SELECT * FROM provider_policy").fetchall()] == \
        [tuple(r) for r in policy_before]
    catalog = {r["model_id"] for r in get_db().execute(
        "SELECT model_id FROM provider_catalog WHERE provider_id = ?", (cid,))}
    assert catalog == {"a", "b", "c"}  # "a" is still in the catalog...


def test_p05_a_refresh_never_resurrects_a_model_the_person_removed(client, serve):
    stub = serve("openai-chat", models=[{"id": "a"}, {"id": "b"}])
    cid = add(client, stub)["connection"]["id"]
    client.delete(f"/api/models/{cid}/models/a")
    for _ in range(3):
        client.post(f"/api/models/{cid}/discover")
    assert [m.model_id for m in store.list_models(cid)] == ["b"]
    assert get_db().execute("SELECT excluded FROM provider_policy WHERE model_id = 'a'").fetchone()[0] == 1
    assert [m["id"] for m in client.get("/api/models").json()["connections"][0]["models"]] == ["b"]


def test_p05_a_refresh_merges_facts_and_does_not_wipe_what_it_left_out(client, serve):
    stub = serve("openai-chat", models=[{"id": "m", "pricing": {"prompt": "0", "completion": "0"},
                                         "supported_parameters": ["tools"]}])
    cid = add(client, stub, gatewayKind="openrouter")["connection"]["id"]
    assert store.get_model(cid, "m").facts == {"free": True, "router": False, "tools": True,
                                               "reasoning": {"supported": False}}
    stub.models = [{"id": "m", "supported_parameters": ["tools", "reasoning"]}]  # a thinner listing
    client.post(f"/api/models/{cid}/discover")
    facts = store.get_model(cid, "m").facts
    assert facts["free"] is True and facts["router"] is False  # kept: not mentioned is not retracted
    assert facts["reasoning"]["supported"] is True  # replaced: reported again, differently


def test_p05_auto_reads_the_join_and_passes_over_removed_and_disabled_models(client, serve):
    stub = serve("openai-chat", models=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
    cid = add(client, stub)["connection"]["id"]
    client.delete(f"/api/models/{cid}/models/a")
    get_db().execute("INSERT INTO provider_policy (provider_id, model_id, enabled) VALUES (?, 'b', 0)", (cid,))
    choose_auto(client)
    assert [c.model.model_id for c in auto.candidates()] == ["c"]
    get_db().execute("UPDATE provider_policy SET user_label = 'My C', user_order = 0 WHERE model_id = 'b'")
    get_db().execute("INSERT INTO provider_policy (provider_id, model_id, user_label) VALUES (?, 'c', 'My C')", (cid,))
    assert store.get_model(cid, "c").label == "My C"  # the person's label over the catalog's


# ================================================================================
# P06 — gateway conventions live in gateway modules, for a declared gateway only
# ================================================================================

GATEWAY_ROWS = [
    {"id": "auto/best", "owned_by": "combo", "pricing": {"prompt": "-1", "completion": "-1"}},
    {"id": "vision/x", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.000001", "completion": "0.000002"}, "supported_parameters": ["tools", "reasoning"]},
]


def test_p06_the_generic_chat_adapter_reports_nothing_beyond_the_ids(client, serve):
    stub = serve("openai-chat", models=GATEWAY_ROWS)
    cid = add(client, stub, gatewayKind="")["connection"]["id"]  # a plain server: nothing declared
    assert [m.facts for m in store.list_models(cid)] == [None, None]
    source = (MODELS / "providers" / "openai_chat.py").read_text(encoding="utf-8")
    for field in ('"owned_by"', '"pricing"', '"architecture"', '"input_modalities"', '"supported_parameters"',
                  '"combo"', '"-1"'):
        assert field not in source, f"the generic chat adapter reads the gateway field {field}"


def test_p06_each_declared_gateway_reads_its_own_fields(client, serve):
    router = serve("openai-chat", models=GATEWAY_ROWS)
    cid = add(client, router, gatewayKind="openrouter")["connection"]["id"]
    assert store.get_model(cid, "auto/best").facts == {"router": True, "free": False}
    assert store.get_model(cid, "vision/x").facts == {
        "chat": True, "tools": True, "image": True, "free": False, "router": False,
        "reasoning": {"supported": True, "levels": list(REASONING_LEVELS), "default": None}}
    omni = serve("openai-chat", models=GATEWAY_ROWS)
    oid = add(client, omni, gatewayKind="omniroute")["connection"]["id"]
    assert store.get_model(oid, "auto/best").facts["router"] is True  # OmniRoute's own signal: owned_by combo
    assert "reasoning" not in store.get_model(oid, "vision/x").facts  # OpenRouter's reading, not OmniRoute's


def test_p06_preferring_routers_is_a_setting_on_the_connection_and_off_by_default(client, serve):
    stub = serve("openai-chat", models=GATEWAY_ROWS)
    cid = add(client, stub, gatewayKind="openrouter")["connection"]["id"]
    choose_auto(client)
    assert sorted(c.model.model_id for c in auto.candidates()) == ["auto/best", "vision/x"]  # off: walk all
    view = client.patch(f"/api/models/{cid}", json={"preferRouters": True}).json()["connection"]
    assert view["preferRouters"] is True and view["gatewayKind"] == "openrouter"
    assert [c.model.model_id for c in auto.candidates()] == ["auto/best"]


def test_p06_a_new_connection_on_openrouters_address_is_declared_openrouter_and_can_be_changed(
        client, monkeypatch):
    from jarvis.models.types import CheckResult
    from jarvis.routes import models as routes

    # Adding a connection tests it; this one must not reach the real internet.
    monkeypatch.setattr(routes, "_run_check", lambda connection: CheckResult(False, "not tested here"))
    body = {"kind": "custom", "format": "openai-chat", "address": "https://openrouter.ai/api/v1", "label": "OR"}
    view = client.post("/api/models", json=body).json()["connection"]
    assert view["gatewayKind"] == "openrouter"
    changed = client.patch(f"/api/models/{view['id']}", json={"gatewayKind": None}).json()["connection"]
    assert changed["gatewayKind"] is None
    refused = client.patch(f"/api/models/{view['id']}", json={"gatewayKind": "some-other-gateway"})
    assert refused.status_code == 400 and "doesn't know a gateway" in refused.json()["error"]
    assert gateways.default_for("http://localhost:20128/v1") is None  # an address is never guessed from


# ================================================================================
# P07 — reasoning is neutral, and each adapter maps it to its own shape
# ================================================================================

REASONS = {"reasoning": {"supported": True, "levels": list(REASONING_LEVELS)}}


def _one(format: str, serve, level: str | None, facts=None) -> dict:
    """One request straight through a provider module; what went on the wire."""
    stub = serve(format)
    module = {"openai-responses": openai_responses, "openai-chat": openai_chat,
              "anthropic-messages": anthropic_messages, "gemini-generatecontent": gemini_generate}[format]
    request = conversation.to_chat_request([{"role": "user", "text": "hi"}], model_id="stub-model-a",
                                           options=GenerationOptions(reasoning=level))
    list(module.stream(Target(stub.base_url, "k"), request, facts=facts))
    return stub.last_body()


def test_p07_each_adapter_maps_a_neutral_level_to_its_own_shape(serve):
    assert _one("anthropic-messages", serve, "maximum", REASONS)["output_config"] == {"effort": "max"}
    assert _one("openai-responses", serve, "balanced", REASONS)["reasoning"] == {"effort": "medium"}
    assert _one("gemini-generatecontent", serve, "thorough", REASONS)["generationConfig"] == {
        "thinkingConfig": {"thinkingBudget": 16384}}
    assert _one("openai-chat", serve, "maximum", REASONS)["reasoning_effort"] == "max"
    assert _one("openai-chat", serve, "minimal", REASONS)["reasoning_effort"] == "low"


def test_p07_a_model_not_reported_to_reason_is_sent_no_reasoning_by_any_adapter(serve):
    keys = {"anthropic-messages": "output_config", "openai-responses": "reasoning",
            "gemini-generatecontent": "generationConfig", "openai-chat": "reasoning_effort"}
    for format, key in keys.items():
        assert key not in _one(format, serve, "thorough", None)  # nothing reported
        assert key not in _one(format, serve, "thorough", {"reasoning": {"supported": False}})  # reported no


def test_p07_discovery_reports_reasoning_in_neutral_names(client, serve):
    anthropic = serve("anthropic-messages", models=[{"id": "thinks", "max_tokens": 1000, "capabilities": {
        "effort": {"supported": True, "low": {"supported": True}, "high": {"supported": True},
                   "xhigh": {"supported": True}}}}])
    aid = add(client, anthropic)["connection"]["id"]
    assert store.get_model(aid, "thinks").facts["reasoning"] == {
        "supported": True, "levels": ["minimal", "thorough"], "default": "thorough"}  # xhigh has no neutral slot
    gemini = serve("gemini-generatecontent", models=[{"id": "g"}])
    gemini._listing = lambda: {"models": [{"name": "models/g", "thinking": True,
                                           "supportedGenerationMethods": ["generateContent"]}]}
    gid = add(client, gemini)["connection"]["id"]
    assert store.get_model(gid, "g").facts["reasoning"]["supported"] is True


def test_p07_auto_only_offers_a_reasoning_request_to_models_reported_to_reason(client, serve):
    stub = serve("openai-chat", models=[{"id": "plain", "supported_parameters": ["tools"]},
                                        {"id": "thinker", "supported_parameters": ["tools", "reasoning"]},
                                        {"id": "silent"}])
    add(client, stub, gatewayKind="openrouter")
    choose_auto(client)
    assert [c.model.model_id for c in auto.candidates()] == ["plain", "thinker", "silent"]
    assert [c.model.model_id for c in auto.candidates(needs_reasoning=True)] == ["thinker"]
    assert [r.model.model_id for r in selection.plan(needs_reasoning=True).attempts] == ["thinker"]


def test_p07_an_effort_saved_under_the_old_names_is_read_as_its_neutral_level(client, serve):
    from jarvis import prefs

    stub = serve("anthropic-messages", models=[{"id": "opus", "max_tokens": 1000, "capabilities": {
        "effort": {"supported": True, "low": {"supported": True}, "max": {"supported": True}}}}])
    cid = add(client, stub)["connection"]["id"]
    select(client, cid, "opus")
    prefs.set_prefs({"selectedEffort": "max"})  # what an earlier version stored
    assert selection.chosen()[2] == "maximum"
    run_step()
    assert stub.last_body()["output_config"] == {"effort": "max"}


# ================================================================================
# P08 — "the model that answered" matches exactly, never by loose prefix
# ================================================================================

def test_p08_gpt_4_is_not_gpt_4o():
    assert not runtime.same_model("gpt-4", "gpt-4o")
    assert not runtime.same_model("gpt-4o", "gpt-4")
    assert not runtime.same_model("claude-x", "claude-x-mini")  # a prefix is not the same model


def test_p08_the_same_id_with_or_without_a_namespace_is_the_same_model():
    assert runtime.same_model("gpt-4o", "gpt-4o")
    assert runtime.same_model("models/gemini-2.5-flash", "gemini-2.5-flash")
    assert runtime.same_model("no-think/cc/claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001")
    assert runtime.same_model("openai/gpt-4o", "gpt-4o")
    assert not runtime.same_model("openai/gpt-4", "gpt-4o")


def test_p08_an_alias_answered_as_its_dated_snapshot_is_the_same_model():
    assert runtime.same_model("claude-sonnet-4-5", "claude-sonnet-4-5-20250929")
    assert runtime.same_model("gpt-4o", "gpt-4o-2024-08-06")
    assert runtime.same_model("llama3", "llama3:latest")
    assert not runtime.same_model("gpt-4", "gpt-4-turbo-2024-04-09")  # "-turbo" is not a date


def test_p08_a_gateways_alias_map_is_opt_in_per_gateway(monkeypatch):
    monkeypatch.setitem(gateways.ALIASES, "openrouter",
                        {"~anthropic/claude-sonnet-latest": frozenset({"anthropic/claude-sonnet-4.5"})})
    assert runtime.same_model("~anthropic/claude-sonnet-latest", "anthropic/claude-sonnet-4.5", "openrouter")
    assert not runtime.same_model("~anthropic/claude-sonnet-latest", "anthropic/claude-sonnet-4.5", "omniroute")
    assert not runtime.same_model("~anthropic/claude-sonnet-latest", "anthropic/claude-sonnet-4.5")


# ================================================================================
# P09 — what Auto does is decided by the scope, and a bad request goes nowhere else
# ================================================================================

INVALID = {"type": "error", "error": {"type": "invalid_request_error", "message": "messages: field required"}}


def test_p09_a_request_scope_error_stops_auto_at_once_and_records_nothing(client, serve):
    bad = serve("anthropic-messages", models=[{"id": "a1"}, {"id": "a2"}], fail_with=(400, INVALID, {}))
    fine = serve("openai-chat", models=[{"id": "fine"}])
    bid = add(client, bad, label="Anthropic")["connection"]["id"]
    add(client, fine, label="Fine")
    choose_auto(client)
    events, error = run_step()
    assert error is not None and "field required" in str(error) and error.detail["scope"] == "request"
    assert len(bad.posts()) == 1 and fine.posts() == []  # no failover: it would be refused the same way
    assert holds_on(bid) == [] and outcome_rows(bid) == 0  # and nothing is held against the model


def test_p09_model_scope_holds_only_that_model_and_auto_tries_the_next(client, serve):
    stub = serve("openai-chat", models=[{"id": "gone"}, {"id": "here"}], unknown_model="gone")
    cid = add(client, stub)["connection"]["id"]
    choose_auto(client)
    events, error = run_step()
    assert error is None and posted_models(stub) == ["gone", "here"]
    assert [(h.scope, h.model_id) for h in holds_on(cid)] == [("model", "gone")]


def test_p09_credential_and_provider_scope_leave_everything_they_reach(client, serve):
    for status, body, scope in ((401, {"error": {"message": "bad key"}}, "credential"),
                                (502, "<html>Bad Gateway</html>", "provider")):
        down = serve("openai-chat", models=[{"id": "d1"}, {"id": "d2"}, {"id": "d3"}], fail_with=(status, body, {}))
        fine = serve("openai-chat", models=[{"id": "fine"}])
        did = add(client, down, label=f"Down-{scope}")["connection"]["id"]
        fid = add(client, fine, label=f"Fine-{scope}")["connection"]["id"]
        choose_auto(client)
        events, error = run_step()
        assert error is None and len(down.posts()) == 1, scope  # one try reached all three
        assert [h.scope for h in holds_on(did)] == [scope]
        client.delete(f"/api/models/{did}")
        client.delete(f"/api/models/{fid}")


def test_p09_an_unknown_scope_is_held_like_one_model_with_exponential_backoff(client, serve):
    stub = serve("openai-chat", models=[{"id": "a"}, {"id": "b"}],
                 fail_with=(400, {"error": {"message": "Provider returned error"}}, {}))
    cid = add(client, stub)["connection"]["id"]
    choose_auto(client)
    events, error = run_step()
    assert error is not None and len(stub.posts()) == 2  # two strikes on the connection, as for model scope
    assert {(h.scope, h.model_id) for h in holds_on(cid)} == {("model", "a"), ("model", "b")}
    [first] = [h for h in holds_on(cid) if h.model_id == "a"]
    lasts = (datetime.fromisoformat(first.until.replace("Z", "+00:00"))
             - datetime.fromisoformat(first.failed_at.replace("Z", "+00:00")))
    assert lasts == timedelta(minutes=5) and first.retry_after_s is None


def test_p09_too_long_for_one_model_is_model_scope_so_auto_can_reach_a_bigger_one(client, serve):
    small = serve("openai-chat", models=[{"id": "small"}], fail_with=(400, {"error": {
        "code": "context_length_exceeded", "message": "This model's maximum context length is 8192 tokens"}}, {}))
    big = serve("openai-chat", models=[{"id": "big"}])
    add(client, small, label="Small")
    add(client, big, label="Big")
    choose_auto(client)
    events, error = run_step()
    assert error is None and posted_models(big) == ["big"]


# ================================================================================
# P11 — adapters take a typed request, built from the stored conversation in one place
# ================================================================================

def test_p11_the_stored_transcript_becomes_typed_parts(serve):
    stored = [
        {"role": "user", "text": "look", "media": [{"kind": "image", "mimeType": "image/png", "dataBase64": "AAA"},
                                                    {"kind": "audio", "mimeType": "audio/wav", "dataBase64": "BBB"},
                                                    {"kind": "image", "uri": "http://nothing-on-hand"}]},
        {"role": "assistant", "text": "calling", "toolCalls": [{"id": "c1", "name": "t", "args": {"x": 1}}],
         "raw": {"adapter": "x", "content": []}},
        {"role": "tool", "toolResults": [{"id": "c1", "name": "t", "result": {"error": "boom"}}]},
        {"role": "narrator", "text": "not a role any model is sent"},
    ]
    request = conversation.to_chat_request(stored, system="sys", tools=[{"name": "t"}], model_id="m")
    assert isinstance(request, ChatRequest) and request.model_id == "m" and request.system == "sys"
    user, assistant, tool = request.messages
    assert user.parts == (TextPart("look"), ImagePart("image/png", "AAA"), MediaPart("audio", "audio/wav", "BBB"))
    assert assistant.parts == (TextPart("calling"), ToolCallPart("c1", "t", {"x": 1}))
    assert assistant.replay == {"adapter": "x", "content": []}
    assert tool.parts == (ToolResultPart("c1", "t", {"error": "boom"}, is_error=True),)
    assert request.tools[0].parameters == {"type": "object", "properties": {}} and request.has_images()


def test_p11_a_reply_cut_off_part_way_is_sent_as_heard_and_never_replayed():
    stored = [{"role": "user", "text": "hi"},
              {"role": "assistant", "text": "the whole long answer", "interrupted": True, "spokenText": "the whole",
               "raw": {"adapter": "anthropic-messages", "content": [{"type": "text", "text": "the whole long answer"}]}}]
    reply = conversation.to_chat_request(stored).messages[1]
    assert reply.parts == (TextPart("the whole"),) and reply.replay is None


def test_p11_no_adapter_reads_a_stored_message_dict():
    """Structural, not a promise: the keys of Jarvis's own stored shape appear nowhere in
    the provider modules, and they may not import the conversation store at all
    (`test_architecture.py`)."""
    # Keys that exist ONLY in Jarvis's stored shape — not in any provider's own payloads.
    stored_keys = ('"toolCalls"', '"toolResults"', '"dataBase64"', '"spokenText"', '"interrupted"',
                   '"media"', '"modelId"')
    for path in (MODELS / "providers").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for key in stored_keys:
            assert key not in source, f"{path.name} reads {key} from a stored message"


def test_p11_generation_options_reach_each_wire_in_its_own_shape(serve):
    def body(format: str) -> dict:
        stub = serve(format)
        module = {"openai-responses": openai_responses, "openai-chat": openai_chat,
                  "anthropic-messages": anthropic_messages, "gemini-generatecontent": gemini_generate}[format]
        request = conversation.to_chat_request(
            [{"role": "user", "text": "hi"}], tools=[{"name": "t", "parameters": {"type": "object"}}],
            model_id="stub-model-a", options=GenerationOptions(temperature=0.2, max_output_tokens=300,
                                                               tool_choice="required", response_format="json"))
        list(module.stream(Target(stub.base_url, "k"), request))
        return stub.last_body()

    chat = body("openai-chat")
    assert (chat["temperature"], chat["max_tokens"], chat["tool_choice"], chat["response_format"]) == (
        0.2, 300, "required", {"type": "json_object"})
    responses = body("openai-responses")
    assert (responses["temperature"], responses["max_output_tokens"], responses["tool_choice"],
            responses["text"]) == (0.2, 300, "required", {"format": {"type": "json_object"}})
    claude = body("anthropic-messages")
    assert (claude["temperature"], claude["max_tokens"], claude["tool_choice"]) == (0.2, 300, {"type": "any"})
    assert "response_format" not in json.dumps(claude)  # Anthropic has no JSON mode: dropped, not refused
    gemini = body("gemini-generatecontent")
    assert gemini["generationConfig"] == {"temperature": 0.2, "maxOutputTokens": 300,
                                          "responseMimeType": "application/json"}
    assert gemini["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
