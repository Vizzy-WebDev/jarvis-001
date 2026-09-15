"""The capability contract (§5) and registry.

Tests concentrate on the six fields the current implementation lacks, and on the
failure modes the audit found in the existing seam.
"""

from __future__ import annotations

import pytest

from jarvis.capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilitySpec,
    DuplicateCapability,
    RetryPolicy,
    Risk,
)


def make(name="demo", **kw) -> CapabilitySpec:
    defaults = dict(
        id=f"builtin.{name}",
        name=name,
        description="does a thing",
        input_schema={"type": "object", "properties": {}},
        risk=Risk.LOW,
        handler=lambda **_: {"ok": True},
    )
    defaults.update(kw)
    return CapabilitySpec(**defaults)


@pytest.fixture
def reg() -> CapabilityRegistry:
    return CapabilityRegistry()


# --- the contract §5 requires ------------------------------------------------

def test_every_field_the_directive_requires_is_present():
    spec = make()
    for required in (
        "id", "name", "description", "input_schema", "risk", "handler",
        "timeout_s", "retry", "cancellable", "result_schema", "tags",
    ):
        assert hasattr(spec, required), f"§5 requires {required}"


def test_risk_is_required_and_must_be_a_real_risk():
    """§7 needs a deterministic permission layer, which is only possible if risk
    is declared rather than inferred. Forgetting it must not default to safe."""
    with pytest.raises(TypeError):
        make(risk="low")  # a bare string is not a Risk


def test_a_capability_always_has_a_timeout():
    """The only tool timeout in the Node system lives in the turn runner, so the
    scheduler, briefings and the Live voice path invoke tools unbounded."""
    assert make().timeout_s > 0
    with pytest.raises(ValueError):
        make(timeout_s=0)


def test_retries_default_to_none():
    """Anything with a side effect must not silently run twice because a caller
    assumed retrying was safe (§49)."""
    assert make().retry.attempts == 0


def test_a_high_risk_capability_cannot_auto_retry():
    """This is how one "send message" becomes three."""
    with pytest.raises(ValueError, match="HIGH risk"):
        make(risk=Risk.HIGH, retry=RetryPolicy(attempts=2))
    # The same policy is fine on a low-risk read.
    make(risk=Risk.LOW, retry=RetryPolicy(attempts=2))


# --- registry behaviour ------------------------------------------------------

def test_register_and_resolve(reg):
    reg.register(make("get_time"))
    assert reg.has("get_time")
    assert reg.require("get_time").name == "get_time"


def test_a_missing_capability_raises_rather_than_returning_none(reg):
    assert reg.get("nope") is None
    with pytest.raises(KeyError):
        reg.require("nope")


def test_a_name_collision_raises_instead_of_silently_shadowing(reg):
    """The Node version lets a built-in shadow a folder Skill of the same name,
    with the only guard consulted at Skill-creation time — so a collision
    introduced any other way vanishes without a word."""
    reg.register(make("research", id="builtin.research"))
    with pytest.raises(DuplicateCapability):
        reg.register(make("research", id="skill.research", kind=CapabilityKind.SKILL))


def test_re_registering_the_same_id_is_an_update_not_a_collision(reg):
    reg.register(make("get_time"))
    reg.register(make("get_time", description="updated"))
    assert reg.require("get_time").description == "updated"


def test_filtering_by_risk(reg):
    reg.register(make("read_file", risk=Risk.LOW))
    reg.register(make("edit_file", risk=Risk.MEDIUM))
    reg.register(make("delete_file", risk=Risk.HIGH))

    assert [s.name for s in reg.list(max_risk=Risk.LOW)] == ["read_file"]
    assert [s.name for s in reg.list(max_risk=Risk.MEDIUM)] == ["edit_file", "read_file"]
    assert len(reg.list(max_risk=Risk.HIGH)) == 3


def test_filtering_by_kind_and_tags(reg):
    reg.register(make("get_time", tags=frozenset({"core"})))
    reg.register(make("run_code", tags=frozenset({"internal"})))
    reg.register(make("research", kind=CapabilityKind.SKILL))

    assert [s.name for s in reg.list(kind=CapabilityKind.SKILL)] == ["research"]
    assert [s.name for s in reg.list(with_tags=["core"])] == ["get_time"]
    assert "run_code" not in [s.name for s in reg.list(without_tags=["internal"])]


def test_declarations_expose_only_what_the_model_needs(reg):
    """Risk stays server-side: §7 requires the permission decision to be
    independent of model behaviour, and a model that can see the risk label is a
    model that can argue with it."""
    reg.register(make("delete_file", risk=Risk.HIGH, tags=frozenset({"core"})))
    decl = reg.declarations()[0]
    assert set(decl) == {"name", "description", "parameters"}
    assert "risk" not in decl
    assert "handler" not in decl


def test_one_enumeration_serves_every_consumer(reg):
    """Three fixed-shape enumerations, none carrying `confirm`, is why deciding
    whether a Skill's pipeline step needs confirmation had to be stitched
    together inside the HTTP route file. Whole specs cannot develop that."""
    reg.register(make("delete_file", risk=Risk.HIGH))
    only = reg.list(max_risk=Risk.HIGH)[0]
    assert only.risk is Risk.HIGH          # the caller can see it
    assert only.timeout_s > 0
    assert only.handler is not None


def test_redact_args_is_declared_on_the_capability(reg):
    """§25: never log secrets. Which arguments are secret is a property of the
    capability, not something each logging call site should have to know."""
    spec = make("connect_service", redact_args=frozenset({"api_key"}))
    assert "api_key" in spec.redact_args
