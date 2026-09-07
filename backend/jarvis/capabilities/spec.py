"""The capability contract (§5) and its risk classification (§7).

Every field §5 lists is here. The ones the current implementation lacks are the
interesting ones:

* **risk** — §7 requires LOW/MEDIUM/HIGH and requires the permission layer to be
  "deterministic" and to "enforce permissions independently of model behavior".
  That is only possible if risk is a declared property of the capability rather
  than something inferred per call. It is therefore required, with no default:
  a new tool cannot accidentally be treated as safe by forgetting to say.

* **timeout_s** — required, with a default. Today the only tool timeout in the
  system lives inside the turn runner, so anything invoked from the scheduler, a
  briefing or the Live voice path runs unbounded. Putting it on the capability
  means every caller inherits it.

* **retry** — §47 asks what happens on failure. A retry policy that lives on the
  capability can express "this is safe to retry" once, rather than each caller
  guessing. Anything with side effects should say `retries=0`.

* **cancellable** — §14 requires "cancel that" to stop work safely. A capability
  that cannot be interrupted must say so, so the orchestrator does not promise
  the user something it cannot deliver.

* **result_schema** — the audit found tool results are "an informal union with no
  schema", with two separate call sites each picking five fields out of it by
  hand. Optional, because not every capability has a machine-checkable shape, but
  present so those that do can declare it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class Risk(str, Enum):
    """§7's classification. Assigned by the capability, never by the model.

    The examples are the directive's own, kept here so a new capability is
    classified against a stated standard rather than an author's intuition.
    """

    #: read a file, search the web, check status, open an application
    LOW = "low"
    #: modify files, install packages, move files, change configuration
    MEDIUM = "medium"
    #: delete files, send messages, purchase, expose credentials, shut down
    HIGH = "high"


class CapabilityKind(str, Enum):
    """§42's distinction, preserved in the data model even though execution is
    unified at one seam."""

    TOOL = "tool"
    SKILL = "skill"
    CONNECTOR = "connector"


@dataclass(frozen=True)
class RetryPolicy:
    """How a failure should be retried, if at all.

    Defaults to NOT retrying. Anything with a side effect must not silently run
    twice because a caller assumed retries were safe — see §49 on idempotency.
    """

    attempts: int = 0            # additional attempts after the first
    backoff_s: float = 0.5
    retry_on_timeout: bool = False

    def __post_init__(self) -> None:
        if self.attempts < 0:
            raise ValueError("retry attempts cannot be negative")


DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: Risk
    handler: Callable[..., Any]
    kind: CapabilityKind = CapabilityKind.TOOL
    timeout_s: float = DEFAULT_TIMEOUT_S
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    cancellable: bool = False
    result_schema: dict[str, Any] | None = None
    #: Free-form labels for routing and visibility, replacing the parallel
    #: boolean fields (`meta`, `internal`, `core`) the current implementation
    #: threads separately through three different enumerations.
    tags: frozenset[str] = frozenset()
    #: §25 — what to log about this capability, and what must never be logged.
    #: Argument names listed here are redacted before any log or trace row.
    redact_args: frozenset[str] = frozenset()
    #: Optional: turns THIS call's arguments into the sentence the user is
    #: actually asked to approve. The policy explains WHY a confirmation is
    #: needed; only the capability can say WHAT will happen in the user's own
    #: terms ("Change 'uses a Mac' to 'uses a Windows PC'?"), and a read-back
    #: that cannot do that is a read-back nobody can meaningfully answer.
    summarize: Callable[[dict[str, Any]], str] | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("a capability needs an id and a name")
        if not isinstance(self.risk, Risk):
            raise TypeError(f"{self.name}: risk must be a Risk, not {type(self.risk).__name__}")
        if self.timeout_s <= 0:
            raise ValueError(f"{self.name}: timeout_s must be positive")
        if self.retry.attempts and self.risk is Risk.HIGH:
            # A high-risk action retried automatically is how one "send message"
            # becomes three. §49's idempotency requirement is not satisfied by
            # hoping the handler is safe.
            raise ValueError(
                f"{self.name}: a HIGH risk capability must not auto-retry "
                "(set retry attempts to 0, or lower the risk if that is wrong)"
            )

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags
