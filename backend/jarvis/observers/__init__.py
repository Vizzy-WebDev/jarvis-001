"""Things that watch what happened, without anything having to tell them.

**This is where the "the orchestrator imports no observer" rule earns its
keep.** Self-capability stats and improvement outcomes are recorded by
subscribing to the event bus, so the turn loop stays ignorant of both — and
`tests/test_architecture.py` asserts that it still is.

The alternative — the turn loop calling a recorder directly — is what made the
original turn loop depend on five subsystems it has no business knowing about,
and untestable without them.
"""

from .recording import start_observers, stop_observers

__all__ = ["start_observers", "stop_observers"]
