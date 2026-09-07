"""Recording what a model call cost, from the event the gateway already publishes.

The turn loop must not know that cost tracking exists — in the Node original it
imported the cost recorder directly, which is one of six imports that made the
loop untestable in isolation. Here the gateway says what happened and this
subscribes.

**Keyed on the presence of `usage`.** The orchestrator publishes the same event
type once per step, without usage, purely to say a step finished. Recording both
would count every turn twice, and the resulting number would look plausible.
"""

from __future__ import annotations

import logging

from ..events.bus import Event

logger = logging.getLogger(__name__)


def record_model_call(event: Event) -> None:
    usage = event.payload.get("usage")
    if not isinstance(usage, dict) or not usage:
        return                       # a step notification, not a usage report

    from ..cost import store

    provider = event.payload.get("provider")
    if not provider:
        return                       # nothing to attribute it to; never guessed at
    store.record_event(
        provider=str(provider),
        model_id=event.payload.get("model") or event.payload.get("modelId"),
        unit_kind="tokens",
        units_in=usage.get("unitsIn"),
        units_out=usage.get("unitsOut"),
        cached_in=usage.get("cachedIn"),
        session_id=event.payload.get("sessionId"),
        background=bool(event.payload.get("background")),
    )
