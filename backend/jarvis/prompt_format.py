"""The one convention shared between the prompt builder and whatever consumes the prompt.

A dependency-free leaf on purpose. `prompt.py` writes this marker between the stable and
volatile halves of the system instruction, and a model client may split on it. Whichever of
the two owned it would be imported by the other, which is a genuine import cycle risk, and a
constant with no imports of its own cannot participate in one.
"""

from __future__ import annotations

#: Everything before it is stable and cacheable; everything after changes per
#: turn. Anthropic's prompt caching keys on an exact prefix, so a per-turn fact
#: on the wrong side of this costs the cache on every single turn.
CACHE_BREAK = "\n\n<<<volatile>>>\n\n"
