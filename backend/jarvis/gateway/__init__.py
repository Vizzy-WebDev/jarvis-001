"""The model gateway — the one place a model call goes through.

In the Node implementation there is no single gateway: `adapter.stream()` is
called from three places (the turn runner, the one-shot helper, and the computer-
control loop), each re-implementing candidate selection, failover, health marking
and availability recording, and two more callers bypass the adapter layer entirely
to construct a provider SDK directly. One of those three never marks health at
all, so a model that fails during computer control is never benched.

This package exists so that logic lives once.
"""

from . import availability, catalog, connections, error_kind, probe, providers, registry, routing
from .client import Gateway, NoModelAvailable
from .routing import Task, build_candidates, explain_exclusions

__all__ = [
    "Gateway",
    "NoModelAvailable",
    "Task",
    "availability",
    "build_candidates",
    "catalog",
    "connections",
    "error_kind",
    "explain_exclusions",
    "probe",
    "providers",
    "registry",
    "routing",
]
