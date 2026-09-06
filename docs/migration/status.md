# Migration status — Next.js + FastAPI

Living progress record for the stack change. Update it as waves land; a
multi-session rewrite this size cannot be tracked from memory.

**Decisions (owner):** build Jarvis only in the new Python stack, `server/`
frozen · full parity, nothing dropped · redesign as I port · fix defects during
the port, disclosed · record-and-compare safety net first · desktop shortcut,
which settles the architecture as one process on one port · relevance-assembled
context (§23) · openWakeWord in the backend · **HIGH-risk actions require fresh
human confirmation even when triggered by scheduled tasks or briefings.**

## Where things stand

| Area | State |
|---|---|
| Safety net (recorder, driver, replay harness) | **Done** |
| Persistence (store, config, db + 21 migrations) | **Done** |
| Chat history (chat store, transcript, session, routes) | **Done** |
| Event bus (§38) · state machine (§37) | **Done** |
| Capability contract (§5) · registry · executor (§45/§47/§49) | **Done** |
| Permissions + persisted approvals (§7/§8/§40) | **Done** |
| Intent router + fast path (§10/§11) | **Done** |
| Orchestrator (§9) | **Done** |
| Model gateway + 3 adapters + probe (§26/§27) | **Done** |
| Built-in tools (§5) | **9 of ~62** — loader, contract and pattern established |
| Architecture fitness functions | **Done** |
| Wake word (§15) · conversation mode (§16) | Not started |
| Context assembler with relevance (§23) | **Seam only** — `WindowContext` |
| Memory · jobs · scheduler · heartbeat · ops · improvement · self | Not started |
| Front end F1–F4 (voice engines, shell, screens, Tailwind) | **Scaffold only** |
| The 15 acceptance tests (§51) | Not started |
| Cutover | Not started |

`cd backend && python -m pytest tests -q` → **260 passed, 40 skipped.** The
skips are contract fixtures for routes not ported yet, so the suite doubles as a
progress meter.

## Defects fixed during the port, disclosed

Each has a test that fails against the original behaviour.

- **The confirmation gate could not fire in voice** (§8). Context is now a
  required, typed argument with no default, so a caller cannot lose its
  protections by omission.
- **A probe proved listing, not generation** (§26). A key that can list is not a
  key that can generate: a gateway saved as keyless and then 401'd on the first
  real question. One minimal generation call now confirms it, and a 401 with no
  key supplied reads as "needs a key", never "that key is invalid".
- **`mini` matched inside "gemini"**, so every Gemini model including Pro scored
  as fast and cheap.
- **The adapter capability table was a hard gate**, making video, audio and web
  search permanently Gemini-only. It seeds a model's own caps now; an explicit
  user value wins.
- **Routing had no concept of `need`**, so the "can it see an image" check lived
  in one caller and was missing from another entirely.
- **Three overlapping availability mechanisms** disagreed about the same failure
  depending on how long the process had been running. One store now, keyed on
  the persisted vocabulary.
- **`EXECUTING -> WAITING_FOR_APPROVAL` was missing** from the lifecycle: one
  step can request several tools and the second can be the one needing a human.
- **`AssistantState.fail()` swallowed a second, different failure reason**,
  hiding the current fault behind the first one.
- **SDK-level retries** hid the 429s the gateway needs in order to bench a model
  and move on. Retry policy belongs to the gateway; both SDKs are constructed
  with `max_retries=0`.

## Verification in place

- **Contract replay** (`tests/test_contract.py`) — 45 recorded exchanges from the
  real Node server; unported routes report as skipped.
- **Store-level differential** (`tests/test_parity_chat_store.py`) — the same
  27-step scenario through both implementations.
- **HTTP-level differential** (`tests/test_parity_http.py`) — boots the real Node
  server on a scratch port and compares a 22-step conversation lifecycle. This
  is what covers MUTATING routes, which fixture replay structurally cannot.
- **A stub server speaking the real wire format** (`tests/stub_openai_server.py`)
  — SSE framing, the empty-`choices` usage chunk and tool-call deltas are parsed
  by the REAL adapter. `tests/test_turn_end_to_end.py` runs a whole turn with
  nothing mocked between the request and the wire.
- **Architecture fitness functions** (`tests/test_architecture.py`) — the seams
  asserted by static import scan rather than remembered.
- **Truth tables** for the policy layer and the state machine.

Run: `cd backend && python -m pytest tests/ -q`

## Open seams — deliberately visible, not forgotten

- `orchestrator/context.py`'s `WindowContext` does no relevance selection and
  says so. The real assembler (§23) needs memory ported first.
- `memory/review.py` — `PORTED = False`, with three real call sites wired
  against it.
- `session_hooks.py` — later modules must register their per-session cleanup or
  "new chat" silently leaks sticky model, unlocked tools and sticky style.
- `GET /api/status` answers `configured: false` unconditionally. Honest for a
  fresh install; must be revisited now that the registry exists.
- `run_code`'s sandbox description must describe the boundary as it actually is
  before that tool is ported (§35/§45).

## Things to carry into later waves

- `control/recording_store` and `screenshot_store` must derive their paths from
  `store.data_dir()`. The Node versions do not — a live test-isolation hole.
- Background engines own timers. Keep every Python scheduler OFF behind a flag
  until cutover, or they act on shared state twice while Node is still running.
- Verify Gemini Live against the INSTALLED Python SDK's source, not its docs.
- Chat streaming moves off SSE-over-GET only AFTER cutover, so the fixture
  harness stays valid across the whole port.
