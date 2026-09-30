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
- **Config is YAML** (`data/models.yaml`, merged over the shipped
  `jarvis/models/data/defaults.yaml`). TOML was ruled out because the standard
  library can read it but not write it, and the settings screen writes this file.
  Saving from the screen rewrites the file, so comments typed into it by hand are
  lost on the next save; the file's header says so.
- **Every config problem is reported at once, each naming where it is**
  (`connections.box.trust: should be one of …`). Nothing invalid is ever written:
  an edit is parsed before it replaces the file. A key-looking entry
  (`api_key`, `Authorization`, …) anywhere in `default_params` is refused — keys
  belong in `.env`.
- **A pin becomes an alias named exactly what the pin says.** Specialists and
  scheduled tasks store a model id as their pin; the migration makes an alias with
  that same name, so those stored values keep working without rewriting any other
  table, and the Specialists screen still shows what the person typed.
- **The migrations (35: move and drop, 36: traces) are registered in the last
  stage of the build**, together with the boundary switch. The person's running
  app loads this working tree on restart; an earlier migration that dropped the
  old tables would have broken the old layer that was still serving them.
- **OpenRouter's preset sets `provider: {allow_fallbacks: false}`** as a default
  param: model and host choice is this layer's job, and a gateway silently
  answering from somewhere else is what "the model that actually answered is
  always recorded" exists to catch.
- **Trust on migration is `local` for Ollama and LM Studio, `standard` for all
  else.** Nothing is ever guessed to be zero-retention; that is the person's claim
  to make in config.
- **A server asking for a long wait is not waited for.** A 429 whose Retry-After is
  over 10 seconds (a free tier's daily quota, typically) moves straight on to the
  next endpoint rather than sleeping; the connection rests for as long as the
  server asked, so other calls skip it too. Shorter waits are honoured.
- **The breaker counts `unavailable`, `timeout` and `auth`**, per endpoint;
  `rate_limited` rests the whole connection instead. Request-shaped errors
  (`invalid_request`, `context_too_long`, `content_refused`) say nothing about the
  endpoint's health and are not counted.
- **"Upstream" defaults to the connection** when config doesn't name one, so
  "prefer a different upstream" means "try another connection first" unless the
  person has said which vendor really serves a gateway's models.
- **`allow_family_change: false` needs both families known.** An endpoint with no
  family can't be shown to be the same family, so it is skipped.
- **`generate()` may fall back after a failed attempt produced partial output**,
  because none of it reached the caller; only `stream()` has a first event to
  protect.
- **Under `optimize: quality`, endpoints outside the route keep config order**; only
  an alias naming a family (which has no order of its own) is ordered by cost,
  then speed.
- **An endpoint on a `local` connection is priced at zero** unless config says
  otherwise: nothing is paid per token on the person's own machine, and it keeps
  local models usable once a monthly budget is spent.
- **Discovery failures are split by kind.** Unreachable/timeout rests the
  connection (`settings.unreachable_rest_s`) so calls skip it; anything else (a key
  refused, a server with no model list) is recorded as the last discovery's error
  and changes nothing else. Either way the last good listing stays.
- **Probing is a CLI command** (`python -m jarvis.models.probe`) that talks to the
  driver directly, bypassing routing — it exists to find out what the catalog
  doesn't know yet, so the catalog can't be allowed to filter it. A case that
  fails for an account reason (auth, rate limit, unreachable) records nothing
  about the capability.
- **A new stub server (`tests/stub_wire.py`) replaces the old one for the
  conformance suite**: the old one couldn't send parallel tool calls, missing ids,
  reasoning items, in-band errors, `/api/show` or embeddings.
- **Anthropic: canonical effort `none` is sent as `low`.** Current Claude models
  can't switch thinking off (a `disabled` thinking config is a 400 on several), so
  the lowest level is the honest nearest. No thinking config is sent at all — each
  model's own default applies — and foreign thinking is dropped, not faked.
- **Gemini: schemas go as JSON Schema** (`parametersJsonSchema`,
  `responseJsonSchema`), so nothing is rewritten. The one known refusal — an array
  with no `items` — makes the endpoint ineligible instead of being "fixed" by
  injecting an `items` (the old behaviour). Not yet confirmed against the live API
  in this build; the first real Gemini call is the check.
- **Gemini: a foreign function call carries Google's documented placeholder
  signature** (`skip_thought_signature_validator`, the `gemini` quirk profile), so
  a conversation can move onto Gemini mid tool loop. Its own signatures are always
  used when present.
- **Responses: `include: ["reasoning.encrypted_content"]` is always sent**, with a
  `no_encrypted_reasoning` quirk for an Open Responses server that refuses it. The
  server's own `call_id` and item id travel in the Sealed `ids` item and go back
  only to that endpoint.
- **`embed()` takes a required `data_class`**, beyond the spec's two arguments:
  "data policy applies" needs to know what the data is. A vector of the wrong
  dimension is refused rather than returned — a different model answering under
  the space's name is exactly the substitution a space exists to prevent.
- **Traces keep no picture bytes even with content switched on** — an image is
  recorded as its type and size. Everything else of the request and response is
  kept when `settings.trace_content` is on.
