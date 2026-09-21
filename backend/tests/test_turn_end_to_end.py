"""A real turn, top to bottom, with no AI model system behind the orchestrator.

While no model system exists every turn must end the same way: a plain, honest
"no model" failure — not a crash, not a hang, and nothing written into the
conversation as though Jarvis had answered.
"""

from __future__ import annotations

import pytest

from jarvis import conversation
from jarvis.capabilities import CapabilityRegistry
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.orchestrator import Orchestrator, TurnRequest
from jarvis.orchestrator.model_port import NoModelClient
from jarvis.orchestrator.pipeline import Failed


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    conversation.reset_for_tests()
    reset_db()


def test_a_turn_with_no_model_fails_plainly_with_a_no_model_code():
    orch = Orchestrator(NoModelClient(), registry=CapabilityRegistry(), event_bus=EventBus())
    events = list(orch.run_turn(TurnRequest(text="hello there", session_id="s1")))

    failures = [e for e in events if isinstance(e, Failed)]
    assert len(failures) == 1
    assert failures[0].code == "no_model"
    assert "no ai model" in failures[0].error.lower()
