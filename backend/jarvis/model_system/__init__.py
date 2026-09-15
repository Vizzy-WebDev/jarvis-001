"""The AI Model System — one internal interface the rest of Jarvis calls to
reach any AI provider, without knowing which one answered.

    caller -> model_system.gateway.execute(AIRequest)
                -> router          (what CAN answer, then what SHOULD)
                -> registry         (what a model IS)
                -> providers/credentials (how to reach it, safely)
                -> adapters/<wire format>  (how to actually call it)
                -> fallback + health (what to do when it fails)
                -> usage            (what happened, for good)

Every piece here is provider-agnostic on purpose. A provider-specific
conditional belongs inside `model_system/adapters/`, one file per WIRE FORMAT — never
scattered through the rest of this package as an `if provider == "..."`.

Nothing under `jarvis/tools/` may import this package, directly or
transitively — the same import invariant `jarvis/tools/CLAUDE.md` states for
the rest of the model system.
"""

from __future__ import annotations
