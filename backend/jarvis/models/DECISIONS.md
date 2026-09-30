# Model layer — decisions

Each entry: what was decided, and why. The specification the layer was rebuilt to
is the source of truth; this file records the choices it left open and the places
it was deliberately corrected.

## Agreed before the build

- **`data_class` is descriptive, not restrictive by default.** Every request states
  it and every trace records it, but the default policy allows every data class on
  every trust class (local, zero-retention, standard). A restriction exists only when
  `policies.data_classes` in config names one; once it does, it is enforced on every
  call — a pin never overrides it, fallback never relaxes it, and a call with no
  allowed endpoint fails in plain language.
- **Three trust classes only** (local, zero_retention, standard). No class for
  free tiers that train on input.
- **Retryable errors retry, then fall back; every other error ends the call** —
  including `auth` (and billing/quota refusals, which map to `auth`) and
  `context_too_long`. Failures are tagged internally only to update the
  connection's rate-limit state and the endpoint's circuit breaker.
- **No empty-reply-as-failure.** A reply that finishes cleanly with nothing in it is
  returned as it is.
- **No fallback after the first content event.** Adaptation warnings are collected
  into the final response, never emitted early — so nothing but content counts as
  "the first event".
- **Effort is a per-request hint** the boundary sends; it is not stored on the
  `selected` alias.
- **Native cache/effort/thinking encoding lives in drivers.** `adapt.py` passes the
  canonical hints (a stable-prefix flag, a canonical effort level) and nothing
  provider-shaped.
- **Traces live in SQLite** (`model_traces`, migration 36), metadata only unless
  `trace_content: true`. Spend queries are GROUP BYs over it. The state file keeps
  only a month-spend counter for the budget check.
- **Connection presets** (what the Add Connection picker offers: driver, default
  address, whether a key is needed, default trust) are data in `data/defaults.yaml`,
  read only by the settings routes and never by routing.
- **Desktop control** builds canonical requests with its data class. The missing
  client wiring in `tools/control_computer.py` is a known pre-existing bug, left
  alone in this build.

## Made during the build

- **The old layer was moved to `models/legacy/` first** so every intermediate commit
  kept a working app; it is deleted when the boundary moves over.
- **Sealed items are opaque and positional.** A driver emits `SealedEvent`s where the
  provider state occurred in its output (a thinking block before the text it
  preceded, a thought signature before the part it belongs to), so replaying the
  output items in order reconstructs the provider's own turn exactly.
- **Canonical tool-call ids are what goes on the wire**, except to the endpoint that
  made the call: a driver whose server has its own ids carries them in a Sealed
  `ids` item (canonical → native) and uses them only when talking to that same
  endpoint. Every other endpoint sees the canonical ids, so a conversation moves
  between families with its ids intact.
- **`jsonschema` and `pyyaml` are declared dependencies.** Both were already in the
  venv (jsonschema through `mcp`); relying on a transitive install is how a fresh
  install breaks.
