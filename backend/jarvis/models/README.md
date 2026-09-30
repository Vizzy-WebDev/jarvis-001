# Jarvis's model layer

Callers ask for what they need; the layer picks the model. A request states a
**task class**, its **content**, the **output** it wants, hard **requirements**,
how sensitive its data is (**data class**) and what to **optimize**. Callers never
name a model or a provider — when they must steer, they use **aliases** from config.

Why it is shaped this way, decision by decision: [DECISIONS.md](DECISIONS.md).

## Public interface (`jarvis.models`)

| Call | Returns |
|---|---|
| `generate(request)` | a complete `Response` |
| `stream(request)` | canonical events, ending in `Done(response)` or one `ErrorEvent` |
| `embed(space, inputs, data_class=...)` | vectors, the space and the model version |
| `explain_route(request)` | eligible endpoints in rank order, and why each other one was rejected — no model is called, and it is the same code path `generate` uses |
| `list_endpoints()` / `refresh_catalog(connection=None)` | the catalog / run discovery now |

The app reaches the layer through two boundary modules only: `client.py` (the turn
loop's port; the one module that imports the orchestrator) and `oneshot.py`
(behind `jarvis.ai.ask`). `settings.py` serves the Model Settings routes.

## Concepts

- **Driver** — translates between the canonical format and one wire protocol:
  `openai_chat` (Chat Completions: OpenRouter, vLLM, LM Studio, Ollama, LiteLLM),
  `openai_responses` (stateless Responses), `anthropic_messages`,
  `gemini_generate`, and `fake` for tests. No routing, no policy.
- **Connection** — a configured instance of a driver: name, driver, base URL,
  `secret_ref` (the key's name in `.env`), trust class (`local`,
  `zero_retention`, `standard`), limits, quirk profile, default params,
  discovery on/off, per-model overrides. Two Ollama machines are two connections.
- **Endpoint** — one model on one connection, `connection/model-id`. What routing
  picks. Capabilities, pricing, family, upstream, health.
- **Alias** — a config name for an endpoint or a family. `selected` is the
  person's chosen model; a specialist's or scheduled task's pin is an alias named
  after the pin.
- **Capabilities** — a fixed, versioned vocabulary (`capabilities.py`); each value
  records whether it was declared, discovered or probed (probed wins).

## A request's path

1. **Resolve** (`resolve.py`) — reject every endpoint that is resting, lacks a
   key, is a trust class the policy doesn't allow for this data class, lacks a
   needed capability, is too small for the request, is over budget or max cost,
   isn't the pin, or can't express a schema. Every reason is recorded. A missing
   strict-schema capability is allowed only for `best_effort` output (emulated).
2. **Route** (`router.py`) — `prefer` aliases, then the task class's route (else
   `default`), then — if the route allows others — everything else; ordered by
   `optimize`; the affinity key's last endpoint first.
3. **Adapt** (`adapt.py`) — sections rendered by the family's prompt profile,
   foreign provider state dropped (and reported), canonical cache/effort hints,
   default params and this driver's extensions.
4. **Execute** (`execute.py`) — retry retryable errors, then fall back within the
   filtered list (another upstream first; never across families when forbidden);
   no fallback after the first streamed content event; limits, breaker, spend.
5. **Finish** (`finish.py`) — output items in order, structured output validated
   (emulated output repaired and buffered), usage and cost, provenance, the
   feature report — and a trace row (`trace.py`).

## Files

| | |
|---|---|
| `data/models.yaml` (data dir) | connections, aliases, routes, policies, embedding spaces, quirk and prompt profiles, settings — the person's config |
| `jarvis/models/data/defaults.yaml` | what the config is merged over, and the Add Connection presets |
| `data/models_state.json` (data dir) | discovery, probe results, latency, health, rests, month spend |
| `model_traces` table | one row per call, metadata only unless `settings.trace_content` |

Config is validated on load; every problem is reported at once with where it is.
Keys are never in config.

## Commands

    python -m jarvis.models.probe <connection/model-id> [--case NAME] [--yes]

Tries an endpoint's capabilities (cases in `data/probe_cases.yaml`), writes what it
measured to the state file, and asks first — showing the estimated cost — before
probing anything not known to be free.

## Adding a server

Add a connection to `models.yaml` (or through the Model Settings screen). If the
server differs from its protocol's norm, describe the difference as a capability,
a quirk flag, a default param or a prompt-profile setting — driver code only when
it is truly about the wire format.
