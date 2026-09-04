"""Memory checkpoints — the seam, ahead of the engine.

The extraction engine lands in Wave 3. This module exists NOW because the
session layer (ported in Wave 1) has three real call sites for it, and wiring
them up later is exactly the kind of thing that gets forgotten: the routes would
work, the tests would pass, and Memory would simply never extract anything, with
nothing anywhere pointing at the omission.

So the call sites are wired for real against this seam, and this seam is loudly
unimplemented rather than quietly absent. `PORTED` is False until the engine
lands, and a test asserts that every caller still routes through here — so
finishing Wave 3 is a matter of filling this in, not of rediscovering who was
supposed to call it.

Nothing is extracted per turn in the original design: extraction is batched into
one model call per CHECKPOINT — a new chat, Jarvis reopening, a scheduled
prompt-task finishing, or the model itself deciding a topic has wrapped up.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Flipped to True when the Wave 3 engine replaces the body below.
PORTED = False


async def checkpoint_conversation(conversation_id: str, reason: str) -> None:
    """Batch-extract memory candidates from a conversation.

    Every call site is FIRE-AND-FORGET by design: a checkpoint must never delay
    the user's own reply or make "New chat" feel slow. Callers must not await
    this in a way that blocks their response, and must swallow its errors.
    """
    if not PORTED:
        logger.debug(
            "memory checkpoint skipped (engine not ported yet): conversation=%s reason=%s",
            conversation_id,
            reason,
        )
        return
    raise NotImplementedError("Wave 3 replaces this body with the real extraction engine.")
