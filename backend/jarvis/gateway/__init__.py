"""The model gateway — the one place a model call goes through.

In the Node implementation there is no single gateway: `adapter.stream()` is
called from three places (the turn runner, the one-shot helper, and the computer-
control loop), each re-implementing candidate selection, failover, health marking
and availability recording, and two more callers bypass the adapter layer entirely
to construct a provider SDK directly. One of those three never marks health at
all, so a model that fails during computer control is never benched.

This package exists so that logic lives once.
"""

from . import (
    availability, connections, deployments, discovery, effort, error_kind, name_guess,
    probe, providers, registry, routing,
)
from .client import Gateway, NoModelAvailable
from .routing import Task, build_candidates, explain_exclusions

__all__ = [
    "Gateway",
    "NoModelAvailable",
    "Task",
    "availability",
    "build_candidates",
    "connections",
    # One model version reached through one connection — the routable unit.
    # Read by nothing yet; the router moves onto it at the switchover.
    "deployments",
    # Asking a provider what it has, and reconciling that with what is
    # configured — including noticing a model that stopped being listed.
    "discovery",
    # Resolving a reasoning level against what a version actually accepts, and
    # remembering what a version has refused.
    "effort",
    "error_kind",
    "explain_exclusions",
    # The name-derived guessing the catalog replaces. Still the live path until
    # the registry and the setup flow stop reading it.
    "name_guess",
    "probe",
    "providers",
    "registry",
    "routing",
]
