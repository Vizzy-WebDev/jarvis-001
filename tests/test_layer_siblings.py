"""Several connections to one provider are separate endpoints: one failing never makes its siblings
or its provider look unavailable, and a capability request weighs every endpoint on every
connection."""

from __future__ import annotations

from jarvis import models
from jarvis.models import state
from jarvis.models.types import Requirements

from layer_helpers import CHAT, ask, configure, fake_conn, layer  # noqa: F401

SEE = {**CHAT, "image_in": True}


def _fleet(layer):
    return configure(layer, [
        fake_conn("gem1", models={"text": {"capabilities": CHAT}, "see": {"capabilities": SEE}}),
        fake_conn("gem2", models={"text": {"capabilities": CHAT}, "see": {"capabilities": SEE}}),
        fake_conn("gem3", models={"text": {"capabilities": CHAT}}),
        fake_conn("ant1", models={"text": {"capabilities": CHAT}, "see": {"capabilities": SEE}}),
        fake_conn("ant2", models={"text": {"capabilities": CHAT}}),
        fake_conn("xai1", models={"grok": {"capabilities": {**CHAT, "audio_in": True}}}),
    ])


def _ranked(**kw):
    return set(models.explain_route(ask(**kw)).ranked)


def test_one_connection_down_leaves_its_siblings_and_every_other_provider(layer):
    _fleet(layer)
    state.mark_connection_down("gem1", "timeout", 300)
    got = _ranked()
    assert not any(e.startswith("gem1/") for e in got)
    assert {"gem2/text", "gem3/text", "ant1/text", "ant2/text", "xai1/grok"} <= got


def test_one_model_resting_leaves_the_same_model_on_a_sibling_connection(layer):
    _fleet(layer)
    state.rest_endpoint("gem1/see", 300, reason="limit")
    got = _ranked()
    assert "gem1/see" not in got and "gem1/text" in got and "gem2/see" in got


def test_a_refused_key_rests_only_that_connection(layer):
    _fleet(layer)
    state.refuse_connection("ant1", "refused", 300)
    got = _ranked()
    assert not any(e.startswith("ant1/") for e in got) and "ant2/text" in got and "gem1/see" in got


def test_a_text_model_down_does_not_hide_an_image_endpoint_on_a_sibling_connection(layer):
    _fleet(layer)
    state.rest_endpoint("gem1/text", 300, reason="limit")
    state.mark_connection_down("gem2", "timeout", 300)
    from jarvis.models.types import ImagePart, Message, TextPart
    req = ask(requirements=Requirements(capabilities=frozenset({"image_in"})))
    got = _ranked(requirements=req.requirements)
    assert got == {"gem1/see", "ant1/see"}


def test_a_capability_only_one_provider_has_is_found_on_it_and_named_when_nobody_has_it(layer):
    _fleet(layer)
    assert _ranked(requirements=Requirements(capabilities=frozenset({"audio_in"}))) == {"xai1/grok"}
    state.mark_connection_down("xai1", "timeout", 300)
    explained = models.explain_route(ask(requirements=Requirements(capabilities=frozenset({"audio_in"}))))
    assert explained.ranked == ()
    by_id = {r.endpoint_id: r for r in explained.rejected}
    assert by_id["xai1/grok"].reason == "connection_unreachable"
    assert "audio in" in by_id["gem1/text"].detail
