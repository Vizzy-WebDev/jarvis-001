"""Authorization — may this action run, right now, for this caller?

Split out of the capability registry deliberately (§7): "The permission layer
must be deterministic. Do not rely solely on the LLM to decide whether something
is safe. The architecture should enforce permissions independently of model
behavior."
"""

from .context import Autonomy, CallContext, Surface
from .decide import Grant, Outcome, PolicyResult, decide

__all__ = [
    "Autonomy",
    "CallContext",
    "Grant",
    "Outcome",
    "PolicyResult",
    "Surface",
    "decide",
]
