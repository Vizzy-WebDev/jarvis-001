"""What a model IS — the vocabulary, and nothing that knows how to call one.

This is the layer the rebuild exists to create. Before it, a model was a name
string and everything else about it was inferred by matching regular
expressions against that string (`gateway/name_guess.py`, still live until the
registry stops reading it). A name is not a fact, and treating it as one is why
an unbounded `mini` matched inside "ge**mini**" and scored every Gemini model,
Pro included, as a cheap fast one.

Three decisions here are load-bearing.

**Capability support has THREE states, not two.** `YES`, `NO`, and `UNKNOWN`
are different, and collapsing the third into either of the others loses real
information: "we have never established whether this model can see an image" is
not "it cannot", and answering it as `NO` silently hides a capable model with
no visible reason, while answering `YES` sends an image to a model that will
fail on it. The old `with_capability_defaults()` had no way to say the third
thing, so every unknown became a confident boolean.

**Effort is declared by a version, never inferred.** A version says which
scheme it speaks and which levels it offers. `EffortKind.UNKNOWN` is a real,
distinct answer from `EffortKind.NONE` for the same reason as above — a model
we have not yet learned anything about is not a model without reasoning
control.

**A family is deliberately thin.** It groups versions and gives a lineage
something to be matched against; it carries no capabilities and no limits. The
facts that matter vary WITHIN a family — one release sees images, the next adds
video — so putting them on the family would mean two places to look for the
truth and a version quietly reporting its family's stale answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Mapping


class Support(Enum):
    """Whether a version can do something.

    `UNKNOWN` is not a placeholder to be resolved before use — it is a value the
    rest of the system is expected to handle, because for most models on most
    capabilities it is the honest answer.
    """

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class Effort(IntEnum):
    """How hard a model is asked to think. The one ladder Jarvis speaks.

    Ordered on purpose: clamping a request down to a version's ceiling is a
    comparison, so these have to be comparable.

    The names are the ones the providers themselves converged on rather than a
    vocabulary invented here — three of the four native schemes already use
    these exact words, so translating is a lookup rather than a mapping anyone
    has to reason about. `OFF` is a real level, not the absence of one: where a
    provider allows thinking to be switched off, that is a different request
    from asking it to think a little.

    The ladder stops at HIGH deliberately. A level above it would be identical
    to HIGH on every tier-based provider, which is a choice in the interface
    that does nothing — the kind of control that teaches people the settings
    don't matter. If a budget-based provider ever justifies a finer top end it
    is additive, and nothing below has to move.
    """

    OFF = 0
    MINIMAL = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4


class EffortKind(Enum):
    """The SHAPE of a version's reasoning control, which differs by provider.

    A single enum of levels cannot express these, which is why the scheme is
    declared rather than assumed:

    * `TIERS`   — a named level on a request field.
    * `BUDGET`  — a number of tokens the model may spend thinking.
    * `VARIANT` — the level selects a DIFFERENT callable model id. Common, and
      the reason `native` values are typed as `Any`: here they are model names.
    * `NONE`    — this version has no reasoning control at all. Most local and
      older models.
    * `UNKNOWN` — nobody has established which of the above is true yet.
    """

    UNKNOWN = "unknown"
    NONE = "none"
    TIERS = "tiers"
    BUDGET = "budget"
    VARIANT = "variant"


class Lifecycle(Enum):
    """Whether this version is still real.

    `RETIRED` exists because a provider dropping a model is otherwise invisible:
    the old build kept calling it, failing, and benching it on a cooldown timer
    forever, rediscovering the same dead end every few hours.
    """

    UNKNOWN = "unknown"
    CURRENT = "current"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class Source(IntEnum):
    """Where a field's value came from.

    Ordered by precedence, low to high, so merging is a comparison. Recorded per
    field rather than per record: a version is almost always a mixture — its id
    discovered, its family matched from a pattern, its context window whatever
    the provider happened to volunteer — and a single flag on the whole object
    cannot say which parts to trust.
    """

    DEFAULT = 0
    CATALOG = 1
    DISCOVERED = 2
    USER = 3


@dataclass(frozen=True)
class Capabilities:
    """What a version can be asked to do. Everything unknown until told."""

    tools: Support = Support.UNKNOWN
    vision: Support = Support.UNKNOWN
    video: Support = Support.UNKNOWN
    audio: Support = Support.UNKNOWN
    web_search: Support = Support.UNKNOWN

    def get(self, name: str) -> Support:
        """Read by capability name, for a caller holding a string.

        An unrecognised name answers `UNKNOWN` rather than raising: a routing
        need that names something this build has never heard of should exclude
        nothing and crash nothing.
        """
        value = getattr(self, name, None)
        return value if isinstance(value, Support) else Support.UNKNOWN


@dataclass(frozen=True)
class EffortScheme:
    """Which reasoning levels a version offers, and what they mean on the wire.

    Invalid combinations are refused at construction rather than left to fail
    at call time — a scheme claiming TIERS with no levels would produce a
    request with an empty parameter, which providers reject in ways that read
    like the model being broken.
    """

    kind: EffortKind = EffortKind.UNKNOWN
    #: The subset of the ladder this version actually offers, lowest first.
    levels: tuple[Effort, ...] = ()
    default: Effort | None = None
    #: Level -> the value this provider wants. A tier name, a token count, or a
    #: model id, depending on `kind`.
    native: Mapping[Effort, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind in (EffortKind.NONE, EffortKind.UNKNOWN):
            if self.levels or self.default is not None:
                raise ValueError(f"{self.kind.value} declares no levels, so it may not list any")
            return
        if not self.levels:
            raise ValueError(f"{self.kind.value} must list the levels it offers")
        if tuple(sorted(self.levels)) != tuple(self.levels):
            raise ValueError("levels must be ordered lowest first")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("levels must not repeat")
        if self.default is None:
            raise ValueError(f"{self.kind.value} must name a default level")
        if self.default not in self.levels:
            raise ValueError(f"default {self.default.name} is not among the offered levels")

    @property
    def controllable(self) -> bool:
        """Whether asking for a level means anything here."""
        return self.kind not in (EffortKind.NONE, EffortKind.UNKNOWN)

    @property
    def ceiling(self) -> Effort | None:
        return self.levels[-1] if self.levels else None

    @property
    def floor(self) -> Effort | None:
        return self.levels[0] if self.levels else None

    def supports(self, level: Effort) -> bool:
        return level in self.levels


#: A version that has told us nothing. Not a placeholder for a real one — this
#: is what most models legitimately are, and the system is built to route them.
UNKNOWN_EFFORT = EffortScheme()


@dataclass(frozen=True)
class Family:
    """A line of models from one provider that share a character.

    Thin on purpose — see this module's docstring. It exists so a person can
    navigate a roster, and so a version released tomorrow can be recognised as
    belonging to a lineage already known.
    """

    id: str
    provider: str
    label: str


@dataclass(frozen=True)
class Version:
    """One concrete, callable release. The unit of truth.

    Identified by (provider, model) rather than by an opaque id: a version is
    not a row someone created, it is a fact about the world, and two people
    discovering the same model must arrive at the same version. The opaque ids
    belong one layer out, on the deployment — the thing a user actually made.
    """

    provider: str
    #: The id the provider's API expects. What actually goes on the wire.
    model: str
    label: str
    family: str | None = None
    #: Whether `model` names one frozen snapshot. A floating alias silently
    #: repoints when the vendor ships a successor, so everything else recorded
    #: here is provisional when this is NO.
    pinned: Support = Support.UNKNOWN
    context_tokens: int | None = None
    capabilities: Capabilities = field(default_factory=Capabilities)
    effort: EffortScheme = UNKNOWN_EFFORT
    #: 0-5, and the only authored number left. Speed and cost were removed in
    #: favour of measurement; this one survives because there is no measurable
    #: proxy for it short of an evaluation harness this build is not growing.
    quality: int | None = None
    lifecycle: Lifecycle = Lifecycle.UNKNOWN
    #: field name -> where its value came from. Absent means DEFAULT.
    provenance: Mapping[str, Source] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.model)

    def source_of(self, field_name: str) -> Source:
        return self.provenance.get(field_name, Source.DEFAULT)

    def is_guessed(self, field_name: str) -> bool:
        """Whether this field is a pattern match rather than something observed.

        The old build recorded one `guessed: True` for a whole record. Per
        field is the useful granularity: a version's id is discovered fact
        while its family is almost always a guess, and a caller that cannot
        tell them apart has to distrust both.
        """
        return self.source_of(field_name) <= Source.CATALOG
