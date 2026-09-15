"""Reasoning/thinking as one normalized dial, translated per model.

The application only ever asks for one of five levels — `Off`, `Low`, `Medium`,
`High`, `Maximum`. Which of those a given model actually offers, and what shape
its own API wants that request in, is a fact about that model, declared in its
`ReasoningScheme` and never guessed.

**A scheme's `kind` is declared, never inferred.** Providers do not agree on
the shape of "think harder": a named tier on a request field, a token budget,
or a completely different model id for the harder-thinking variant. Sending
the wrong shape to a provider produces a rejected request that reads like the
model being broken, so the adapter never chooses — it is handed a resolved
`ReasoningRequest` and only translates it.

**Clamping goes DOWN, never up, and is always reported.** A request for
`MAXIMUM` against a model whose ladder stops at `HIGH` is lowered to `HIGH`;
never raised, and never silent — a clamp nobody can see is indistinguishable
from the control not working at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Mapping


class Effort(IntEnum):
    """The one ladder this system speaks. Ordered, because clamping a request
    to a model's ceiling is a comparison."""

    OFF = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    MAXIMUM = 4


class ReasoningKind(Enum):
    """The SHAPE of a model's reasoning control.

    * `TIERS`   — a named level on a request field.
    * `BUDGET`  — a number of tokens the model may spend thinking.
    * `VARIANT` — the level selects a different callable model id.
    * `NONE`    — this model has no reasoning control at all.
    * `UNKNOWN` — nobody has established which of the above is true.
    """

    UNKNOWN = "unknown"
    NONE = "none"
    TIERS = "tiers"
    BUDGET = "budget"
    VARIANT = "variant"


@dataclass(frozen=True)
class ReasoningScheme:
    """Which levels a model offers, and what each means on that model's wire.

    Invalid combinations are refused at construction rather than left to fail
    at call time — a `TIERS` scheme with no levels would build a request with
    an empty parameter, which providers reject in ways that look like the
    model itself being broken.
    """

    kind: ReasoningKind = ReasoningKind.UNKNOWN
    levels: tuple[Effort, ...] = ()
    default: Effort | None = None
    #: level -> the value this provider's wire actually wants: a tier name, a
    #: token count, or a model id, depending on `kind`.
    native: Mapping[Effort, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind in (ReasoningKind.NONE, ReasoningKind.UNKNOWN):
            if self.levels or self.default is not None:
                raise ValueError(f"{self.kind.value} declares no levels, so it may not list any")
            return
        if not self.levels:
            raise ValueError(f"{self.kind.value} must list the levels it offers")
        if tuple(sorted(self.levels)) != tuple(self.levels) or len(set(self.levels)) != len(self.levels):
            raise ValueError("levels must be ordered, lowest first, with no repeats")
        if self.default is None or self.default not in self.levels:
            raise ValueError("default must be one of the offered levels")

    @property
    def controllable(self) -> bool:
        return self.kind not in (ReasoningKind.NONE, ReasoningKind.UNKNOWN)

    @property
    def ceiling(self) -> Effort | None:
        return self.levels[-1] if self.levels else None

    @property
    def floor(self) -> Effort | None:
        return self.levels[0] if self.levels else None

    def supports(self, level: Effort) -> bool:
        return level in self.levels

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "default": self.default.name if self.default is not None else None,
            "native": {level.name: self.native.get(level) for level in self.levels},
        }


#: A model that has told us nothing. Not a placeholder for a real scheme —
#: this is what most models legitimately are until discovery or a user says
#: otherwise, and the router has to work correctly with it.
UNKNOWN_SCHEME = ReasoningScheme()
#: A model whose maker has confirmed it takes no reasoning parameter at all.
NO_REASONING = ReasoningScheme(kind=ReasoningKind.NONE)


def scheme_from_dict(value: Any) -> ReasoningScheme | None:
    """Read a scheme from stored JSON, or `None` if there isn't a usable one.
    Never raises — a hand-edited or corrupt record degrades to "we do not
    know" rather than taking the registry down."""
    if value is None:
        return None
    if isinstance(value, ReasoningScheme):
        return value
    if not isinstance(value, dict):
        return None
    try:
        kind = ReasoningKind(str(value.get("kind", "")).lower())
    except ValueError:
        return None
    if kind in (ReasoningKind.NONE, ReasoningKind.UNKNOWN):
        return ReasoningScheme(kind=kind)

    native: dict[Effort, Any] = {}
    for name, native_value in (value.get("native") or {}).items():
        try:
            native[Effort[str(name).upper()]] = native_value
        except KeyError:
            continue
    if not native:
        return None

    levels = tuple(sorted(native))
    try:
        default = Effort[str(value.get("default")).upper()]
    except (KeyError, AttributeError):
        default = levels[len(levels) // 2]
    if default not in levels:
        default = levels[len(levels) // 2]
    try:
        return ReasoningScheme(kind=kind, levels=levels, default=default, native=native)
    except ValueError:
        return None


def clamp(requested: Effort, scheme: ReasoningScheme) -> tuple[Effort | None, bool]:
    """The nearest level this scheme can actually serve, and whether it moved.

    `None` means there is nothing to send at all — the scheme offers no
    control, or nobody has established that it does.
    """
    if not scheme.controllable or not scheme.levels:
        return None, False
    if scheme.supports(requested):
        return requested, False
    below = [level for level in scheme.levels if level <= requested]
    chosen = below[-1] if below else scheme.floor
    return chosen, chosen is not requested


@dataclass(frozen=True)
class ReasoningRequest:
    """A level resolved against one model's own terms — what an adapter
    actually translates onto its wire. It never chooses a level and never
    clamps; both already happened here."""

    level: Effort
    scheme: ReasoningScheme
    requested: Effort
    clamped: bool

    @property
    def kind(self) -> ReasoningKind:
        return self.scheme.kind

    @property
    def native(self) -> Any:
        return self.scheme.native.get(self.level)


def plan(requested: Effort | None, scheme: ReasoningScheme) -> ReasoningRequest | None:
    """What to actually ask this model for, or `None` to ask for nothing.

    `None` covers every reason not to send anything: the model has no
    reasoning control, nobody has established whether it does, or the caller
    asked for nothing specific and the model has no default worth sending.
    """
    target = requested if requested is not None else scheme.default
    if target is None:
        return None
    level, was_clamped = clamp(target, scheme)
    if level is None:
        return None
    return ReasoningRequest(level=level, scheme=scheme, requested=target, clamped=was_clamped)
