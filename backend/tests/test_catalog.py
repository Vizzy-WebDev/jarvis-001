"""The catalog: what a model is, resolved from three sources under one precedence.

The test that matters most here is the empty one. A catalog that only works
when it happens to contain your model is a hardcoded model list with extra
steps, and the whole point of this layer is that it degrades to "unknown" and
keeps working. So the empty-seed path is exercised first and deliberately, not
as an edge case.

The rest guard the two defects that made the old table unfixable: it was keyed
on exact model ids that provider listings never return, and it had no way to
say "we do not know".
"""

from __future__ import annotations

import pytest

from jarvis.catalog import (
    Capabilities, Effort, EffortKind, EffortScheme, Lifecycle, Source, Support,
    looks_pinned, match, resolve,
)
from jarvis.catalog.known import SEED


# --- the binding constraint ------------------------------------------------

def test_an_empty_catalog_still_produces_a_usable_version():
    """No seed, no discovery, no user input — just an id nobody recognises.

    This is the specification of the whole layer. If it fails, the catalog has
    become a lookup table that other code cannot function without.
    """
    version = resolve(model="some-model-nobody-ships", rules=())

    assert version.model == "some-model-nobody-ships"
    assert version.label == "some-model-nobody-ships"
    assert version.provider == "unknown"
    assert version.family is None
    assert version.quality is None
    assert version.context_tokens is None
    assert version.lifecycle is Lifecycle.UNKNOWN


def test_an_empty_catalog_answers_unknown_rather_than_guessing():
    """Every capability unanswered, and none of them quietly turned into a no.

    The old `with_capability_defaults()` filled absent flags from the adapter's
    ceiling and forced False for local and gateway connections, so "nobody has
    established this" was indistinguishable from "established: it cannot".
    """
    version = resolve(model="some-model-nobody-ships", rules=())

    for name in ("tools", "vision", "video", "audio", "web_search"):
        assert version.capabilities.get(name) is Support.UNKNOWN

    assert version.effort.kind is EffortKind.UNKNOWN
    assert version.effort.controllable is False
    assert version.effort.levels == ()
    assert version.effort.ceiling is None


def test_the_whole_resolution_path_runs_with_no_rules_at_all():
    """Not just the defaults — user input and discovery still merge correctly
    when the shipped catalog contributes nothing."""
    version = resolve(
        model="mystery-7b",
        provider="somebody",
        discovered={"context_tokens": 8000, "capabilities": {"tools": True}},
        user={"label": "My local model"},
        rules=(),
    )

    assert version.label == "My local model"
    assert version.provider == "somebody"
    assert version.context_tokens == 8000
    assert version.capabilities.tools is Support.YES
    assert version.capabilities.vision is Support.UNKNOWN


# --- the defect that made the old table unfixable --------------------------

def test_a_dated_snapshot_still_finds_its_family():
    """The exact failure of `KNOWN.get(model)`.

    Provider listings return dated ids. An exact-match table keyed on bare
    names misses every one of them and falls through to name guessing, which is
    why the old table could not be repaired by adding entries.
    """
    bare = resolve(model="claude-sonnet-5")
    dated = resolve(model="claude-sonnet-5-20251001")

    assert bare.family == "claude-sonnet"
    assert dated.family == "claude-sonnet", "a dated snapshot must match its lineage"
    assert dated.effort.kind is bare.effort.kind


def test_a_version_released_tomorrow_lands_in_its_family_with_no_edit_here():
    """A pattern matches a lineage, so a future release needs no new entry."""
    future = resolve(model="claude-opus-9-20991231")

    assert future.family == "claude-opus"
    assert future.provider == "anthropic"


def test_a_gateway_prefixed_id_still_matches():
    """Aggregators serve `maker/model`. The lineage is the maker's either way."""
    version = resolve(model="anthropic/claude-haiku-4-5")

    assert version.family == "claude-haiku"
    assert version.provider == "anthropic"


def test_the_model_id_is_the_label_of_last_resort_not_the_family_name():
    """Four releases of one family must stay distinguishable in a picker."""
    version = resolve(model="claude-opus-5-20250101")

    assert version.label == "claude-opus-5-20250101"
    assert version.family == "claude-opus"


# --- precedence ------------------------------------------------------------

def test_a_user_override_beats_everything():
    version = resolve(
        model="claude-sonnet-5",
        discovered={"context_tokens": 111},
        user={"context_tokens": 999, "family": "my-own-grouping", "quality": 1},
    )

    assert version.context_tokens == 999
    assert version.family == "my-own-grouping"
    assert version.quality == 1
    assert version.source_of("context_tokens") is Source.USER
    assert version.source_of("family") is Source.USER


def test_what_the_provider_reports_beats_what_we_shipped_knowing():
    version = resolve(model="claude-sonnet-5", discovered={"context_tokens": 250_000})

    assert version.context_tokens == 250_000
    assert version.source_of("context_tokens") is Source.DISCOVERED


def test_the_shipped_catalog_fills_only_what_nobody_else_answered():
    version = resolve(model="claude-sonnet-5")

    assert version.family == "claude-sonnet"
    assert version.source_of("family") is Source.CATALOG
    assert version.source_of("context_tokens") is Source.DEFAULT


def test_a_discovered_capability_does_not_erase_the_ones_beside_it():
    """Per-flag precedence, not per-object.

    A provider that reports only vision must not wipe a tools answer that came
    from the catalog — merging whole objects is how that happens.
    """
    version = resolve(
        model="claude-sonnet-5",
        discovered={"capabilities": {"vision": True}},
    )

    assert version.capabilities.vision is Support.YES
    assert version.source_of("capabilities.vision") is Source.DISCOVERED
    assert version.capabilities.tools is Support.YES, "the catalog's answer must survive"
    assert version.source_of("capabilities.tools") is Source.CATALOG


def test_a_source_saying_unknown_is_not_an_answer():
    """`UNKNOWN` from a higher-precedence source keeps the search going rather
    than settling the field as unknown."""
    version = resolve(
        model="claude-sonnet-5",
        discovered={"capabilities": {"tools": Support.UNKNOWN}},
    )

    assert version.capabilities.tools is Support.YES
    assert version.source_of("capabilities.tools") is Source.CATALOG


# --- provenance ------------------------------------------------------------

def test_provenance_is_recorded_per_field_not_per_record():
    """One flag on a whole version cannot describe a mixture, and a version is
    always a mixture."""
    version = resolve(
        model="claude-sonnet-5-20251001",
        discovered={"context_tokens": 200_000},
        user={"label": "Workhorse"},
    )

    assert version.source_of("label") is Source.USER
    assert version.source_of("context_tokens") is Source.DISCOVERED
    assert version.source_of("family") is Source.CATALOG


def test_a_pattern_match_is_reported_as_a_guess_and_a_discovered_fact_is_not():
    version = resolve(model="claude-sonnet-5", discovered={"context_tokens": 200_000})

    assert version.is_guessed("family") is True
    assert version.is_guessed("context_tokens") is False


# --- floating aliases ------------------------------------------------------

def test_a_dated_id_reads_as_pinned_and_a_bare_one_does_not():
    """A floating alias repoints when the vendor ships a successor, so
    everything recorded against it is provisional."""
    assert looks_pinned("claude-sonnet-5-20251001") is Support.YES
    assert looks_pinned("claude-sonnet-5") is Support.NO
    assert looks_pinned("") is Support.UNKNOWN


def test_the_user_can_correct_a_pin_reading():
    """It is a read of the id's shape, and a provider may name things any way
    it likes — so it must be overridable."""
    version = resolve(model="weird-name-2024", user={"pinned": False})

    assert version.pinned is Support.NO
    assert version.source_of("pinned") is Source.USER


# --- the effort scheme -----------------------------------------------------

def test_the_ladder_is_ordered_so_a_ceiling_can_be_clamped_to():
    assert (Effort.OFF < Effort.MINIMAL < Effort.LOW < Effort.MEDIUM
            < Effort.HIGH < Effort.MAX)


def test_versions_from_different_providers_offer_different_ladders():
    """The reason clamping is real work rather than a formality.

    Read out of the installed SDKs: the OpenAI-shaped wire accepts a value
    above `high`, and Gemini's level enum does not — so two models a user might
    switch between genuinely cannot be asked the same question.
    """
    gpt = resolve(model="gpt-5.6-luna").effort
    gemini = resolve(model="gemini-3-pro").effort

    assert gpt.ceiling is Effort.MAX
    assert gemini.ceiling is Effort.HIGH
    assert gpt.supports(Effort.OFF) is True
    assert gemini.supports(Effort.OFF) is False, "Gemini's level enum has no off"


def test_every_level_a_scheme_offers_has_something_to_send():
    """A level with no native value would put an empty parameter on the wire,
    which providers reject in ways that read like the model being broken."""
    for rule in SEED:
        for level in rule.effort.levels:
            assert level in rule.effort.native, (
                f"{rule.family} offers {level.name} with no native value")


def test_a_scheme_that_offers_nothing_may_not_claim_levels():
    with pytest.raises(ValueError):
        EffortScheme(kind=EffortKind.NONE, levels=(Effort.LOW,), default=Effort.LOW)


def test_a_scheme_that_offers_levels_must_name_a_default_among_them():
    with pytest.raises(ValueError):
        EffortScheme(kind=EffortKind.TIERS, levels=(Effort.LOW, Effort.HIGH))
    with pytest.raises(ValueError):
        EffortScheme(kind=EffortKind.TIERS, levels=(Effort.LOW,), default=Effort.HIGH)


def test_levels_must_be_ordered_and_distinct():
    """Ordering is what makes `ceiling` and `floor` meaningful rather than
    whatever the author happened to type first."""
    with pytest.raises(ValueError):
        EffortScheme(kind=EffortKind.TIERS, levels=(Effort.HIGH, Effort.LOW),
                     default=Effort.LOW)
    with pytest.raises(ValueError):
        EffortScheme(kind=EffortKind.TIERS, levels=(Effort.LOW, Effort.LOW),
                     default=Effort.LOW)


def test_no_reasoning_control_is_a_real_answer_and_not_the_same_as_unknown():
    """Two distinct facts. Collapsing them means either never asking a capable
    model to think, or asking one that cannot."""
    none = EffortScheme(kind=EffortKind.NONE)
    unknown = EffortScheme(kind=EffortKind.UNKNOWN)

    assert none.controllable is False and unknown.controllable is False
    assert none.kind is not unknown.kind


def test_a_budget_scheme_carries_numbers_and_a_tier_scheme_carries_names():
    """The shapes genuinely differ, which is why the scheme is declared."""
    sonnet = resolve(model="claude-sonnet-5").effort
    gpt = resolve(model="gpt-5.6-luna").effort

    assert sonnet.kind is EffortKind.BUDGET
    assert isinstance(sonnet.native[Effort.MEDIUM], int)
    assert gpt.kind is EffortKind.TIERS
    assert isinstance(gpt.native[Effort.MEDIUM], str)


# --- the seed itself -------------------------------------------------------

def test_every_shipped_rule_is_internally_consistent():
    """The rules are data, and data with no test is data that drifts."""
    for rule in SEED:
        assert rule.provider and rule.family and rule.label
        if rule.effort.controllable:
            assert rule.effort.default in rule.effort.levels
            assert set(rule.effort.native) >= set(rule.effort.levels)
        if rule.quality is not None:
            assert 0 <= rule.quality <= 5


#: A real id per shipped family, in the dated form a provider listing actually
#: returns. Written out rather than generated from the patterns, because a
#: sample derived from the thing it is testing proves nothing.
FAMILY_EXAMPLES = {
    "claude-opus": "claude-opus-5-20250101",
    "claude-sonnet": "claude-sonnet-5-20251001",
    "claude-haiku": "claude-haiku-4-5-20251001",
    "gemini-pro": "gemini-3-pro",
    "gemini-flash": "gemini-3.6-flash",
    "gpt": "gpt-5.6-luna",
}


def test_every_shipped_pattern_matches_a_real_id_from_its_family():
    """A rule whose pattern matches nothing is dead weight that reads as
    coverage. Every family ships with an example it must recognise."""
    assert set(FAMILY_EXAMPLES) == {rule.family for rule in SEED}, (
        "a family was added or renamed without an example to test its pattern")

    for family, example in FAMILY_EXAMPLES.items():
        assert resolve(model=example).family == family, (
            f"{family}'s pattern does not match {example}")


def test_no_shipped_pattern_claims_a_model_from_another_family():
    """Patterns are matched in order and the first wins, so an over-broad one
    silently steals its neighbours' models."""
    for family, example in FAMILY_EXAMPLES.items():
        matched = [rule.family for rule in SEED if rule.pattern.search(example)]
        assert matched == [family], f"{example} matched {matched}, expected only {family}"


def test_no_shipped_rule_claims_a_capability_it_cannot_know_per_version():
    """A rule describes a LINE of models. Vision, video and audio change
    between releases of one family, so asserting them here would be stating a
    per-version fact at the wrong level."""
    for rule in SEED:
        for name in ("vision", "video", "audio", "web_search"):
            assert rule.capabilities.get(name) is Support.UNKNOWN, (
                f"{rule.family} asserts {name} for a whole lineage")


def test_matching_returns_nothing_for_an_unrecognised_id():
    assert match("totally-unknown-thing") is None


# --- what arrives from outside ----------------------------------------------

HOSTILE = [
    "not a mapping at all", 12, ["a", "list"],
    {"capabilities": "not a mapping"},
    {"capabilities": {"vision": "maybe", "no_such_capability": True}},
    {"capabilities": {"vision": None}},
    {"lifecycle": 12},
    {"effort": [1, 2]},
    {"effort": {"kind": "tiers"}},
    {"quality": "five"},
    {"quality": 9999},
    {"quality": True},
    {"context_tokens": {"a": 1}},
    {"context_tokens": -5},
    {"provider": None},
]


@pytest.mark.parametrize("nonsense", HOSTILE)
def test_a_malformed_field_degrades_to_unknown_rather_than_raising(nonsense):
    """`overrides` reaches this from a PATCH body, and resolution runs at READ
    time on every routing pass. A value that raises here does not spoil one
    request — it empties the roster, and with it the models screen, the status
    route and every turn. Degrading to "we do not know" is the only safe answer.
    """
    version = resolve(model="some-model", discovered=nonsense, user=nonsense)

    assert version.model == "some-model"
    assert version.capabilities.vision is Support.UNKNOWN
    assert version.lifecycle is Lifecycle.UNKNOWN
    assert version.quality is None
    assert version.context_tokens is None


def test_a_quality_outside_the_scale_is_refused_rather_than_clamped():
    """Not a stricter validation for its own sake. `routing._score` multiplies
    quality by up to 3 and the availability bonus is sized against the spread
    that produces — a quality of 9999 from a hand-edited file would outrank the
    bonus and put a model known to be dead at the front of every turn."""
    assert resolve(model="m", user={"quality": 5}).quality == 5
    assert resolve(model="m", user={"quality": 0}).quality == 0
    assert resolve(model="m", user={"quality": 6}).quality is None
    assert resolve(model="m", user={"quality": -1}).quality is None


def test_a_refused_value_is_not_reported_as_having_come_from_the_user():
    """Provenance says where a value came from. A field that was thrown away
    has no value to have come from anywhere, and saying `user` about one would
    make a screen offer to "correct" a setting that is not in effect."""
    version = resolve(model="m", user={"quality": 9999, "context_tokens": -5})

    assert "quality" not in version.provenance
    assert "context_tokens" not in version.provenance
