"""What a model IS — the vocabulary, and nothing that knows how to call one.

This is the layer the rebuild exists to create. Before it, a model was a name
string and everything else about it was inferred by matching regular
expressions against that string, in a module called `gateway/name_guess.py`
that was deleted at the switchover once nothing read it any more. A name is not
a fact, and treating it as one is why an unbounded `mini` matched inside
"ge**mini**" and scored every Gemini model, Pro included, as a cheap fast one.

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

import re
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
    vocabulary invented here, so translating is a lookup rather than a mapping
    anyone has to reason about. Read out of the installed SDKs rather than
    remembered, because this is exactly the kind of detail that rots:

    * the OpenAI-shaped `reasoning_effort` accepts
      none / minimal / low / medium / high / xhigh / max
    * Gemini's `ThinkingLevel` enum offers MINIMAL / LOW / MEDIUM / HIGH
    * Anthropic takes a token budget, which is continuous and so maps to
      whatever ladder sits above it

    MINIMAL through HIGH is therefore the span every one of them can express,
    and the ends are where they differ.

    `OFF` is a real level, not the absence of one: switching thinking off is a
    different request from asking for a little, and the OpenAI-shaped wire has
    a distinct value for it.

    `MAX` exists because two of the three can genuinely go beyond HIGH. An
    earlier draft of this file stopped at HIGH, arguing that anything above it
    would be indistinguishable on tier-based providers — the SDKs say
    otherwise, so the argument was wrong and the level is here. Gemini's level
    enum has no equivalent, which is not a problem to solve: a Gemini version
    simply does not list MAX among its levels, and a request for it clamps to
    that version's ceiling. That is the clamp earning its place rather than
    being hypothetical.

    `xhigh` is deliberately not a rung. One provider's intermediate step does
    not need to become vocabulary everything else has to pretend to have — and
    a version that wants it can map its own MAX onto it, since the native
    values are per-version data rather than a table here.
    """

    OFF = 0
    MINIMAL = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    MAX = 5


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


#: The boundary in a camelCase name, so `webSearch` reads as `web_search`.
_SNAKE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


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

        `webSearch` and `web_search` are the same capability. The routing
        `need` dict is populated from names that travel over the wire and is
        therefore camelCase, while the fields here are Python; normalising in
        the one place a capability is read by string is what stops a real
        requirement from silently answering `UNKNOWN` and excluding nothing.

        An unrecognised name still answers `UNKNOWN` rather than raising: a
        routing need that names something this build has never heard of should
        exclude nothing and crash nothing.
        """
        value = getattr(self, self.normalise(name), None)
        return value if isinstance(value, Support) else Support.UNKNOWN

    @staticmethod
    def normalise(name: str) -> str:
        """`webSearch` and `web_search` spell the same capability."""
        return _SNAKE.sub("_", name).lower() if name else ""


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

    def as_dict(self) -> dict[str, Any]:
        """A plain, JSON-safe form.

        Needed because these facts are stored: a scheme learned from a
        provider's parameter list is written to a deployment's record, and a
        scheme a user sets by hand arrives as JSON from a browser. Both ends of
        that round trip have to be ordinary data.

        Levels are keyed by NAME rather than by their integer value — JSON has
        no integer keys, and a file someone opens should say `"MEDIUM"` rather
        than `"3"`.
        """
        return {
            "kind": self.kind.value,
            "default": self.default.name if self.default is not None else None,
            "native": {level.name: self.native.get(level) for level in self.levels},
        }


def effort_scheme_from(value: Any) -> EffortScheme | None:
    """Read a scheme from a stored record, or None if there isn't one.

    Accepts an `EffortScheme` unchanged so a caller holding a real one does not
    have to care, and a dict as written by `as_dict`. Anything malformed answers
    None rather than raising: this reads user-editable stored data, and a
    hand-mangled file should degrade to "we do not know" rather than stop the
    app from starting.
    """
    if value is None:
        return None
    if isinstance(value, EffortScheme):
        return value
    if not isinstance(value, dict):
        return None
    try:
        kind = EffortKind(str(value.get("kind", "")).lower())
    except ValueError:
        return None
    if kind in (EffortKind.NONE, EffortKind.UNKNOWN):
        return EffortScheme(kind=kind)

    native: dict[Effort, Any] = {}
    for name, native_value in (value.get("native") or {}).items():
        try:
            native[Effort[str(name).upper()]] = native_value
        except KeyError:
            continue
    if not native:
        return None

    default_name = value.get("default")
    try:
        default = Effort[str(default_name).upper()] if default_name else None
    except KeyError:
        default = None
    levels = tuple(sorted(native))
    if default not in levels:
        default = levels[len(levels) // 2]
    try:
        return EffortScheme(kind=kind, levels=levels, default=default, native=native)
    except ValueError:
        return None


#: A version that has told us nothing. Not a placeholder for a real one — this
#: is what most models legitimately are, and the system is built to route them.
UNKNOWN_EFFORT = EffortScheme()


@dataclass(frozen=True)
class EffortRequest:
    """A reasoning level resolved against one version's terms.

    Lives here rather than with the code that produces it because an ADAPTER is
    its consumer, and an adapter cannot import from the gateway — the gateway
    imports adapters, so the dependency only runs one way. Keeping this beside
    the scheme it is resolved against is also simply where it belongs: it is a
    value, not a step.

    `requested` is carried alongside `level` so a caller can say what it did. A
    clamp nobody can see is indistinguishable from the setting being ignored,
    which is how a control teaches people it does not work.
    """

    level: Effort
    scheme: EffortScheme
    requested: Effort
    clamped: bool

    @property
    def kind(self) -> EffortKind:
        return self.scheme.kind

    @property
    def native(self) -> Any:
        """What this provider wants on the wire for this level."""
        return self.scheme.native.get(self.level)


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

    def as_dict(self) -> dict[str, Any]:
        """A plain, JSON-safe form, for a route or a screen.

        Declared here rather than built in the route module for the same reason
        `EffortScheme.as_dict` is: a version is the thing whose shape matters,
        and a serialiser that lives beside a handler ends up being written a
        second time the next route that needs one.

        `capabilities` keeps all three states as their own names — flattening
        `UNKNOWN` into `false` on the way to a screen would put the old build's
        confident-boolean problem back at the last possible moment, where the
        interface says "cannot see images" about a model nobody has asked.

        `provenance` travels too, because "we matched this from the name" and
        "the provider told us" look identical once they are both just values,
        and the first is the one a person may want to correct.
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "label": self.label,
            "family": self.family,
            "pinned": self.pinned.value,
            "contextTokens": self.context_tokens,
            "capabilities": {name: getattr(self.capabilities, name).value
                             for name in ("tools", "vision", "video", "audio", "web_search")},
            "effort": self.effort.as_dict(),
            "quality": self.quality,
            "lifecycle": self.lifecycle.value,
            "provenance": {name: source.name.lower()
                           for name, source in self.provenance.items()},
        }

    def is_guessed(self, field_name: str) -> bool:
        """Whether this field is a pattern match rather than something observed.

        The old build recorded one `guessed: True` for a whole record. Per
        field is the useful granularity: a version's id is discovered fact
        while its family is almost always a guess, and a caller that cannot
        tell them apart has to distrust both.
        """
        return self.source_of(field_name) <= Source.CATALOG
