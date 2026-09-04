# Migration status — Next.js + FastAPI

Living progress record for the stack change. Update it as waves land; a
multi-session rewrite this size cannot be tracked from memory.

**Decisions (owner):** full parity, nothing dropped · build fresh alongside, no
strangler proxy · record-and-compare safety net first · desktop shortcut, which
settles the architecture as one process on one port.

## Where things stand

| Phase | State |
|---|---|
| 0. Safety net (recorder, driver, replay harness) | **Done** |
| 1. Python foundation (store, config, db + 19 migrations) | **Done** |
| 2 · Wave 1. Stateless CRUD routes | **In progress** — 10 of ~147 |
| 2 · Wave 2. Turn engine (atomic) | Not started |
| 2 · Wave 3. Background engines | Not started |
| 2 · Wave 4. Platform bridges | Not started |
| 2 · Wave 5. Real-time (SSE + the two WebSockets) | Not started |
| 3 · F0. Front-end scaffold | **Done** |
| 3 · F1. Voice engines + orb, ported as TS classes | Not started |
| 3 · F2. Shell | Not started |
| 3 · F3. Screens (22 files) | Not started |
| 3 · F4. style.css → Tailwind | Not started |
| 4. Cutover | Not started |

## Ported routes (10)

`GET /api/status` · `GET|POST /api/prefs` · `GET|POST /api/conversations` ·
`GET|PATCH|DELETE /api/conversations/{id}` ·
`POST /api/conversations/{id}/activate` · `POST /api/reset`

`GET /api/status` currently answers `configured: false` unconditionally. That is
honest for a fresh install with no key, which is all the fixtures cover, but it
MUST be revisited when the models registry lands in Wave 2.

## Ported modules

`store` · `config` · `db` + `migrations` · `jscompat` · `prefs` · `chat_store` ·
`conversation` · `session` · `session_hooks` · `memory/review` (seam only)

## Verification in place

- **Contract replay** (`tests/test_contract.py`) — 45 recorded exchanges from the
  real Node server. Unported routes report as skipped, so the suite doubles as a
  progress meter.
- **Store-level differential** (`tests/test_parity_chat_store.py`) — the same
  27-step scenario through both implementations, transcripts must match.
- **HTTP-level differential** (`tests/test_parity_http.py`) — boots the real Node
  server on a scratch port and compares a 22-step conversation lifecycle. This is
  what covers MUTATING routes, which fixture replay structurally cannot.
- **Id/timestamp formats** (`tests/test_id_formats.py`) — pinned separately,
  because the contract normaliser hides values and therefore also shapes.

Run: `cd backend && python3 -m pytest tests/ -q`

## Open seams — deliberately visible, not forgotten

- `memory/review.py` — `PORTED = False`. Three real call sites are wired against
  it; Wave 3 fills in the body.
- `session_hooks.py` — Wave 2 modules must register their per-session cleanup
  (sticky model, unlocked tools, sticky style) or "new chat" silently leaks them.
- `GET /api/status` — see above.

## Things to carry into later waves

- `control/recording_store` and `screenshot_store` must derive their paths from
  `store.data_dir()`. The Node versions do not, which is a live test-isolation
  hole — see findings.md.
- Wave 3's background engines own timers. Keep every Python scheduler OFF behind
  a flag until cutover, or they act on shared state twice while the Node app is
  still running.
- Verify Gemini Live against the INSTALLED Python SDK's source, not its docs.
  This project has been burned by Gemini docs describing an API the SDK did not
  actually expose.
- Chat streaming moves off SSE-over-GET only AFTER cutover, so the fixture
  harness stays valid across the whole port.
