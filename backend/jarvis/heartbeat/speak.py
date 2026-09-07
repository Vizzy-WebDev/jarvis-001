"""Jarvis starting a turn nobody asked for.

A genuinely new channel: until this, nothing in the app could speak without a
message from the user first. It is one function, deliberately — everything about
whether speaking is appropriate was decided before getting here, so this only
has to do it properly:

* a real assistant message on the active session, so it is part of the
  conversation rather than a popup with no history;
* an event, so an open UI can play it aloud;
* the outbox row marked delivered, because actually saying it out loud IS the
  resolving action for this path. (The next-turn fallback is different: being
  shown to the model is not the same as having been said, and that row is marked
  only when something acts on it.)
"""

from __future__ import annotations

import logging

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus

logger = logging.getLogger(__name__)


def speak_now(text: str, *, outbox_id: int | None = None, reason: str | None = None,
              event_bus: EventBus | None = None) -> str:
    from .. import conversation
    from ..session import get_active_session_id
    from . import outbox

    ebus = event_bus or default_bus
    session_id = get_active_session_id()
    conversation.push_assistant_text(session_id, text)
    ebus.publish(EventType.ASSISTANT_RESPONSE, {
        "sessionId": session_id, "proactive": True, "text": text, "reason": reason})
    if outbox_id is not None:
        outbox.mark_delivered(outbox_id)
    logger.info("[heartbeat] said something first: %s", text[:80])
    return session_id
