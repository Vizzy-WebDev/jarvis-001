"""The one convention shared between the prompt builder and an adapter.

A dependency-free leaf on purpose. `prompt.py` writes this marker and
`adapters/anthropic_adapter.py` splits on it, so whichever of the two owned it
would be imported by the other — and since the adapters are imported by the
gateway, which is imported by the orchestrator, which builds the prompt, that is
a genuine import cycle (found by it actually failing, not by inspection).

A constant with no imports of its own cannot participate in one.
"""

from __future__ import annotations

#: Everything before it is stable and cacheable; everything after changes per
#: turn. Anthropic's prompt caching keys on an exact prefix, so a per-turn fact
#: on the wrong side of this costs the cache on every single turn.
CACHE_BREAK = "\n\n<<<volatile>>>\n\n"
