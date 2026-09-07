"""Replay the recorded Node contract against the FastAPI port.

Every fixture under tests/contract/fixtures/ is a real exchange captured from the
running Node server by tools/record/proxy.mjs. A route counts as ported only once
it answers the recorded request the same way the Node server did.

Routes that have not been ported yet are reported as SKIPPED, not failed — the
suite is a live progress meter for the migration as well as a regression guard,
and a wall of red for work not yet started would just train everyone to ignore
it. What is never skipped is a route that EXISTS in the port: once it answers at
all, it must answer correctly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contract.normalize import normalise_exchange

FIXTURE_DIR = Path(__file__).parent / "contract" / "fixtures"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))

# How much normalisation is too much. A comparison that only passes because
# dozens of values were waved away is not really passing; this ceiling makes
# that visible rather than invisible. Raise it only with a reason.
MAX_NORMALISED_FIELDS = 40

#: Keys this build ADDS to a recorded response, per route, because the feature
#: behind them does not exist in the Node app at all.
#:
#: Every entry is a deliberate, named divergence and nothing more: the recorded
#: keys must still be present and byte-identical, so this can never hide a
#: changed or dropped value — only an added one. A new preference is the one
#: shape of divergence a port that is also building new things cannot avoid, and
#: editing the RECORDING to accommodate it would quietly destroy the thing the
#: recording is for.
#: Routes this build has deliberately NOT ported yet, with the reason. They
#: report as skipped, exactly like a route nobody has started — the difference is
#: that this one is a decision somebody made, written down where it will be read
#: next time rather than rediscovered.
DEFERRED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/connectors/catalog"):
        "the connector directory is entirely OAuth flows, which land with the front end",
}

#: Rows a recorded list legitimately does not have here yet, by connector type.
#: The rest of the list is still compared exactly, so this can hide a missing
#: feature by name but never a changed one. Empty now: the browser connector was
#: the last entry here, and it landed with the desktop work.
ABSENT_CONNECTOR_TYPES: set[str] = set()

ADDED_KEYS: dict[tuple[str, str], set[str]] = {
    # Semantic verification of consequential chat answers: this build's own
    # feature, off by default. See jarvis/ops/consequence.py.
    ("GET", "/api/prefs"): {"verifyChatAnswers"},
    # What the screen-watching badge is lit FOR. The original could only say
    # that something was watching; several things can be (a glance, a monitor,
    # sharing left on), and which one it is decides whether "stop" means
    # anything to the person reading it.
    ("GET", "/api/observation/status"): {"reasons"},
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ported_paths(app) -> set[tuple[str, str]]:
    """(METHOD, path-template) pairs the port actually registers.

    Read from the app's own OpenAPI schema rather than by walking app.routes:
    FastAPI nests included routers behind a private wrapper type, so an
    isinstance check against APIRoute silently finds nothing and every fixture
    skips itself. The schema is public API and gives the templates already in
    "{param}" form, which is exactly what matching needs.

    Used instead of inferring "not ported yet" from a 404 response. That
    inference is wrong for the seven fixtures that legitimately EXPECT a 404:
    an unported route and a correctly-404ing route are indistinguishable by
    status alone, so the seven most valuable not-found fixtures would silently
    skip themselves exactly when they started mattering.
    """
    schema = app.openapi()
    pairs = set()
    for path, operations in schema.get("paths", {}).items():
        for method in operations:
            pairs.add((method.upper(), path))
    return pairs


def _matches_a_ported_route(ported: set[tuple[str, str]], method: str, path: str) -> bool:
    """Whether `path` is served by a registered route, allowing for {params}."""
    for route_method, template in ported:
        if route_method != method.upper():
            continue
        t_parts = template.strip("/").split("/")
        p_parts = path.strip("/").split("/")
        if len(t_parts) != len(p_parts):
            continue
        if all(t.startswith("{") or t == p for t, p in zip(t_parts, p_parts)):
            return True
    return False


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """A client bound to an isolated data directory.

    The fixtures were recorded against an empty instance, so the port must be
    replayed against an empty one too — pointing this at real data would compare
    an empty-state recording against a populated response and fail for the wrong
    reason entirely.
    """
    import os

    data_dir = tmp_path_factory.mktemp("contract-data")
    env_path = tmp_path_factory.mktemp("contract-env") / ".env"
    os.environ["JARVIS_DATA_DIR"] = str(data_dir)
    os.environ["JARVIS_ENV_PATH"] = str(env_path)

    from jarvis import db as db_module
    from jarvis import session as session_module
    from jarvis import store as store_module
    from jarvis.main import create_app

    db_module.reset_for_tests()
    store_module.reset_for_tests()
    session_module.reset_for_tests()
    app = create_app()
    with TestClient(app) as c:
        c.ported_routes = _ported_paths(app)
        yield c
    db_module.reset_for_tests()
    store_module.reset_for_tests()
    session_module.reset_for_tests()


def _fixture_id(path: Path) -> str:
    return path.stem


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=_fixture_id)
def test_route_matches_recorded_node_response(client, fixture_path):
    fixture = _load(fixture_path)
    req = fixture["request"]
    expected = fixture["response"]

    if expected.get("kind") == "sse":
        pytest.skip("SSE contract is verified as an event sequence in Wave 5, not by body replay")

    deferred = DEFERRED_ROUTES.get((req["method"].upper(), req["path"]))
    if deferred:
        pytest.skip(f"deliberately not ported yet: {req['method']} {req['path']} — {deferred}")

    if not _matches_a_ported_route(client.ported_routes, req["method"], req["path"]):
        pytest.skip(f"not ported yet: {req['method']} {req['path']}")

    url = req["path"] + (f"?{req['query']}" if req.get("query") else "")
    response = client.request(req["method"], url, json=req.get("body") or None)

    got, got_norm = normalise_exchange(
        response.status_code, dict(response.headers), _json_or_text(response)
    )
    want, want_norm = normalise_exchange(
        expected["status"], expected.get("headers", {}), expected.get("body")
    )

    assert got_norm.count <= MAX_NORMALISED_FIELDS, (
        f"{got_norm.count} fields normalised away for {req['path']} — the comparison "
        f"is being hollowed out: {got_norm.fields[:15]}"
    )
    assert got["status"] == want["status"], f"status differs for {req['method']} {req['path']}"
    assert got["headers"] == want["headers"], f"contract headers differ for {req['path']}"
    if req["path"] == "/api/connectors" and ABSENT_CONNECTOR_TYPES \
            and isinstance(want["body"], dict):
        want["body"] = {**want["body"], "connectors": [
            c for c in want["body"].get("connectors", [])
            if c.get("type") not in ABSENT_CONNECTOR_TYPES]}

    added = ADDED_KEYS.get((req["method"].upper(), req["path"]))
    if added and isinstance(got["body"], dict) and isinstance(want["body"], dict):
        extra = set(got["body"]) - set(want["body"])
        assert extra == added, (
            f"{req['path']} added {sorted(extra)}, but only {sorted(added)} is a declared "
            "divergence — add it to ADDED_KEYS deliberately or take it back out")
        got_body = {k: v for k, v in got["body"].items() if k not in added}
        assert got_body == want["body"], f"body differs for {req['method']} {req['path']}"
    else:
        assert got["body"] == want["body"], f"body differs for {req['method']} {req['path']}"


def _json_or_text(response):
    try:
        return response.json()
    except ValueError:
        return {"_text": response.text[:4000]}


def test_fixtures_exist():
    """A harness with no fixtures passes vacuously — which is worse than failing."""
    assert FIXTURES, "no contract fixtures recorded; run tools/record/proxy.mjs + drive.mjs"


# A credential prefix on its own proves nothing — 'sk-' occurs inside the
# perfectly innocent path '/api/task-runs', which is what the first version of
# this check tripped on. Each pattern therefore requires the credential-shaped
# TAIL as well, not just the prefix.
CREDENTIAL_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),        # OpenAI-style
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),       # Google API key
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),    # Anthropic
    re.compile(r"\bxoxb-[A-Za-z0-9-]{20,}"),       # Slack bot token
    re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),  # JWT
]


def test_no_fixture_contains_a_secret():
    """Fixtures are committed. Credentials must never reach one."""
    for path in FIXTURES:
        text = path.read_text(encoding="utf-8")
        for pattern in CREDENTIAL_PATTERNS:
            match = pattern.search(text)
            assert match is None, (
                f"{path.name} looks like it contains a real credential "
                f"matching {pattern.pattern}"
            )


def test_the_secret_scan_actually_catches_a_secret():
    """Guards the guard: a scanner that can never fire is worse than none.

    Verifies both directions — a real-shaped key is caught, and the innocent
    path that broke the first version of this check is not."""
    planted = 'API key: sk-' + 'a1b2c3d4e5' * 3
    assert any(p.search(planted) for p in CREDENTIAL_PATTERNS)
    assert not any(p.search('{"path": "/api/task-runs"}') for p in CREDENTIAL_PATTERNS)
