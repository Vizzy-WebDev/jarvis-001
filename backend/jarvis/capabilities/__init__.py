"""Capabilities — what the assistant can actually do.

§42 requires four concepts stay distinct: a TOOL is an atomic capability, a SKILL
is a reusable capability composed from tools, a JOB is long-running execution, an
AGENT is a reasoning worker. The Node implementation gets this right in one
important way worth preserving: tools, folder Skills and connector tools are
genuinely three different things in their DATA MODEL (different storage, load
timing, permission model, naming) that are normalised to one runnable shape at a
single seam. Execution is unified; identity is not.

What it lacks is the contract §5 specifies. Of the twelve required fields, six
are missing entirely: risk level, per-tool timeout, retry policy, cancellation
support, result schema, and logging metadata. The consequences are real and were
verified: the only tool timeout in the system lives in the turn runner, so the
scheduler, briefings and the Live voice path invoke tools with no timeout at all;
and there is no risk classification, so the permission layer has nothing
deterministic to reason about.
"""

from .spec import CapabilityKind, CapabilitySpec, Risk, RetryPolicy
from .registry import CapabilityRegistry, DuplicateCapability, registry

__all__ = [
    "CapabilityKind",
    "CapabilitySpec",
    "CapabilityRegistry",
    "DuplicateCapability",
    "RetryPolicy",
    "Risk",
    "registry",
]
