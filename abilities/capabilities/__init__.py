"""Capabilities — what the assistant can actually do.

§42 requires four concepts stay distinct: a TOOL is an atomic capability, a SKILL
is a reusable capability composed from tools, a JOB is long-running execution, an
AGENT is a reasoning worker. Tools, folder Skills and connector tools are three
different things in their DATA MODEL (different storage, load timing, permission
model, naming) that are normalised to one runnable shape at a single seam.
Execution is unified; identity is not.

The contract §5 specifies is `CapabilitySpec` (`spec.py`): risk level, per-tool
timeout, retry policy, cancellation support, result schema and logging metadata,
enforced for every caller by `execute.py` and decided deterministically by
`policy/decide.py`.
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
