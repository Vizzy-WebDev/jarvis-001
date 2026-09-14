"""The catalog: what a model IS, independently of how it is reached.

Two axes cross to make something callable. This one describes the model —
provider, family, version, what it can do, what reasoning control it offers.
The other is the connection: an address and a credential. Neither contains the
other, which is what lets one model be reachable through two connections at
different prices, and one gateway connection serve models from several makers.

Read-only and free of side effects: no store, no network, no imports from the
registry, the router or the gateway. It answers questions; it owns nothing.

Nothing imports this yet. It is built and tested standalone first so that the
change introducing it cannot break a running install, and so the empty-catalog
guarantee is established before anything depends on it.
"""

from __future__ import annotations

from .known import SEED, BUDGET_TOKENS, FamilyRule, match
from .merge import looks_pinned, resolve
from .spec import effort_scheme_from
from .spec import (
    UNKNOWN_EFFORT,
    Capabilities,
    Effort,
    EffortKind,
    EffortRequest,
    EffortScheme,
    Family,
    Lifecycle,
    Source,
    Support,
    Version,
)

__all__ = [
    "BUDGET_TOKENS",
    "Capabilities",
    "Effort",
    "EffortKind",
    "EffortRequest",
    "EffortScheme",
    "Family",
    "FamilyRule",
    "Lifecycle",
    "SEED",
    "Source",
    "Support",
    "UNKNOWN_EFFORT",
    "Version",
    "effort_scheme_from",
    "looks_pinned",
    "match",
    "resolve",
]
