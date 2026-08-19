# CLAUDE.md

Jarvis: a local voice/text assistant. Node.js + Express server, plain ES-module
front-end (no build step), runs only on `127.0.0.1`. Non-technical end user — keep
error messages and setup steps in plain language.

## Run it

```
Start Jarvis.bat        # what the user double-clicks: npm install (if needed) + launch + open browser
npm install && npm start   # equivalent, for dev
```

Server listens on `127.0.0.1:3000`. `.env` (git-ignored) holds API keys/secrets, written
by `server/config.js` — never hand-edit the format, use `saveSecret()` (generic) or
`saveProviderKey()` (the three legacy provider aliases only).

**Before killing/restarting the node process, check whether the user already has an
instance running and is actively using it** (`ps aux | grep node`). This project is
tested interactively in the browser by the user, not just by us — killing their server
mid-session has happened before and is disruptive. Prefer testing on a separate port or
via static analysis over restarting their instance.

**The user may have a second Claude session working in this repo at the same time** —
files can appear mid-session, and a test port can already be occupied by the other
session's server. Put new work in new files, re-read any shared file immediately before
editing it, use small targeted `Edit`s rather than `Write` on anything shared, and pick
an unusual test port. **A restart that "succeeds" can still be talking to a stale
process** — `netstat -ano | findstr :PORT` (or `Get-NetTCPConnection -LocalPort PORT`)
is the only reliable check; a working `curl` afterward can still be answered by an old
process a failed kill attempt didn't actually reach.
`Stop-Process -Id (Get-NetTCPConnection -LocalPort PORT).OwningProcess -Force` is what
actually reaches it.

## No automated test suite

Verification is manual:
- `node --check <file>` for syntax (run across all changed files before calling
  something done) — also catches an accidental `require()` inside an ES module, which
  throws immediately rather than lazily (`server/**/*.js` is all `type: "module"`)
- `curl` against server endpoints directly (see any adapter's `testConnection` for the pattern)
- The `agent-browser` skill for UI testing (navigate, snapshot, screenshot, console) — prefer
  it over asking the user to click through things themselves during development
- Pure-logic modules (e.g. `turn-detector.js`) can be tested directly with a one-off
  `node --input-type=module -e "..."` script — no server needed

**Testing must never touch the user's real `data/`/`.env`/port.** `store.js` and
`config.js` both support `JARVIS_DATA_DIR`/`JARVIS_ENV_PATH` overrides, `server.js`
supports `PORT` — set all three for a test run. When a test genuinely needs a real,
already-configured model, it's safe to point `JARVIS_ENV_PATH` at the user's **real**
`.env` (reading a secret doesn't touch anything) while still using a scratch
`JARVIS_DATA_DIR` + a copied, trimmed `connections.json`/`models.json` + a separate
`PORT` — real key, fully isolated everything else. A module that hardcodes a path
relative to its own source file (`__dirname`) bypasses this entirely — use `store.js`'s
`dataDir()` export for any new `data/` subdirectory instead. A `node:http` stub model
(canned JSON keyed on the incoming prompt, SSE chunks for `stream:true`, ~150 lines) is
the way to verify a pipeline when quota is gone — all of the user's models being
rate-limited is the normal state, not an edge case; register the stub in the scratch
data dir only.

Use the `Bash` tool's own `run_in_background: true` for anything that must outlive a
single tool call (a scratch test server, in particular) — a background process started
with plain shell `&`/`disown` does not reliably survive past that tool call; it can
silently stop responding with no error at the point it dies, and everything downstream
then hangs or times out looking exactly like a client-side bug.

A POSIX-style scratch path (`/c/Users/...`) silently mangles into `C:\c\Users\...` when
embedded in a JS string literal a plain Windows `node.exe` parses itself
(`node -e "...fs.writeFileSync('$SCRATCH/...')"`) — Bash's own path translation only
applies to arguments Bash itself recognizes as paths, never to text inside a `-e`
string Node parses on its own. Keep a second, Windows-style variable
(`C:/Users/...`, forward slashes are fine) for anything landing inside a JS string
literal passed to `node -e`; use the POSIX one only for Bash's own commands.

## Structure

```
server/
  server.js          Express app + all routes. Binds 127.0.0.1 only. PORT overridable via env (testing).
  config.js          .env read/write: legacy per-provider keys + generic secrets (getSecret/saveSecret/deleteSecret).
                      ENV_PATH overridable via JARVIS_ENV_PATH (testing).
  store.js           Atomic JSON read/write for data/*.json. DATA_DIR overridable via JARVIS_DATA_DIR (testing).
  brain.js           Thin dispatcher -> models/runner.js; owns the active conversation id (see "Chat Persistence")
  prefs.js           data/prefs.json: autoSelect, balance, manualModelId, clarifySensitivity
  conversation.js    Neutral, model-agnostic transcript store (see "Model system" below and "Chat Persistence")
  clarify.js         Voice-confidence gate for the "ifUnclear" confirm tier
  turn-check.js      classifyTurnComplete — always Gemini directly, independent of active model
  prompt.js          Shared system instruction (was duplicated 4x pre-refactor); injects approved memories (see "Memory")
  tts.js             Gemini text-to-speech -> WAV buffer (always Gemini, independent of active model)
  live.js            WebSocket proxy (/api/live) to Gemini Live, via ai.live.connect()
  events.js          SSE hub (/api/events) for server -> browser push
  db.js              The one SQLite connection (Chat History + Memory), migrations (see "Chat Persistence")
  chat-store.js      Conversation/message CRUD + full-text search over SQLite (see "Chat Persistence")
  memory/            Memory Manager, policy seam, checkpoint engine (see "Memory")
  profile.js         Thin adapter over Memory's "About You" category — data/profile.json is gone (see "Memory")
  adapters/          One module per wire format (see "Model system")
  models/            Registry, connections, routing, health, execution (see "Model system")
  scheduler/         Tasks, recurrence, briefing (see "Scheduler + briefing")
  skills/            Auto-loaded tool implementations (below); skills/store/ holds folder-Skill install logic
  connectors/        MCP/API/CLI/browser/files connector mechanisms (see "App Control connectors")
  documents/         Word/Excel/PowerPoint -> Markdown reader, no new dependency (see server/documents/CLAUDE.md)
  monitor/           "Watch for X, then act" background checks (see "Monitoring")
  sandbox/           Isolated code execution: wsl/restricted backends (see "Sandbox")
  control/           Computer control: PowerShell bridge, the perceive/decide/act loop, safety (see "Computer control")
  ai.js              One-off model call with fallback + JSON extraction (see "One-off model calls")
  gemini-key.js      Resolves a Gemini key from the legacy env var OR any gemini connection
  media.js           Shared file-kind/mime helpers, article fetch, small-file inlining (see "Content Analysis")
  attachments.js     Files attached in chat -> inline media / document text / registered content
  research.js        Real web research: free search+read path, model-native search (see "Research")
  uploads.js         data/uploads/: raw-body upload landing + pruning; id-addressed, ids are untrusted
  projects/          Project store, assistant list, the project engine (see "Planning Partner") — NO screen, NO routes
  content/           Content store, free-glance intake, the investigator (see "Content Analysis") — NO screen, NO routes
public/              Front-end shell + screens + voice engines + the orb — see public/CLAUDE.md
  app.js             UI shell + drawer/router mount + stream-event handling
  nav.js             SECTIONS registry (drawer + router + voice nav all read from this)
  router.js          Hash router (#/models etc.) over the existing screen-switching primitive
  screens/           One file per drawer section, plus shared _modal.js/_helpers.js
  engines/           PipelineEngine (any model) and LiveEngine (Gemini Live), same interface
  turn-detector.js   Adaptive silence-wait timing + mic energy monitoring
  audio-player.js    Sentence-chunked TTS playback queue (Gemini voice output)
  browser-speaker.js Same queue interface, using the browser's built-in speechSynthesis
  orb.js             The 3D orb (idle/listening/thinking/speaking)
  voice-envelope.js  Offline amplitude envelope of Jarvis's own TTS audio, for the orb (never touches real playback)
  notifications.js   Toast + bell/popover + full history, backed by server/notifications.js
  dictation.js       Composer-only dictation mic, separate from the voice-control engines
  vendor/three/      Vendored three.js — the one deliberate front-end dependency
  settings.js        localStorage-backed UI prefs
data/                (git-ignored) JSON persistence — models, connections, prefs, tasks, task-runs, briefing —
                     plus jarvis.db (SQLite): Chat History + Memory (see those sections). profile.json is
                     gone, migrated into Memory.
```

## Model system (server/adapters/ + server/models/)

Three layers, each with one job:

**Adapters** (`server/adapters/{gemini,anthropic,openai-compatible}.js`) — one per wire
format, not per model. `openai-compatible.js` alone covers OpenAI, Ollama, LM Studio,
OpenRouter, Groq — anything OpenAI-shaped — by pointing `baseUrl` elsewhere. Each exports
`stream(entry, messages, opts)` (async generator over the neutral message format below),
`testConnection(entry)`, `listModels(entry)` (throws on failure — registry.js's
`discoverModels()` catches it and turns it into `{models, error}`), `friendlyError(err)`.

**Connections + models** (`server/models/connections.js` + `registry.js`) — a
"connection" is one saved address+key (`data/connections.json`); a "model" is one model
name under a connection (`data/models.json`, holding only `connectionId` plus its own
label/caps/tier — `registry.listModels()`/`getModel()` hydrate in the connection's
adapter/baseUrl/secretRef at read time). Several models discovered together share one
connection instead of each duplicating the same key. `updateModel()` whitelists its
patch keys on purpose — never let adapter/baseUrl/secretRef/connectionId be set through
it, or a model desyncs from its connection. Legacy secrets (the original
`gemini`/`anthropic`/`openai` refs) are never deleted by a connection removal —
`tts.js`/`turn-check.js` depend on `GEMINI_API_KEY` regardless of which model is
chatting. **`assumesVision()` (`catalog.js`) does not treat every non-local cloud model
as vision-capable** — aggregator hosts (openrouter/groq/together) are matched by
name-hint only, same as local models, since discovery-guessed quality scores are
unreliable signals for what a model can actually see.

**Routing + execution** (`router.js`, `health.js`, `runner.js`) — `router.js` ranks
enabled+healthy+ready models by a task profile and the balance dial. `health.js` is an
in-memory circuit breaker (5-min cooldown) — a failed model is skipped, then
auto-retried once its cooldown expires. `runner.js`'s `runTurn(sessionId, text, opts)`
builds the candidate list (`opts.modelId` — a one-off pin, e.g. from a scheduled task —
beats the user's global manual pick, which beats pure auto-ranking), tries each in
order, replays the same neutral transcript on failure so context survives a model
switch, and yields `model_switch`/`restart`/`paused` events the UI turns into "switching
models" notices or a plain "nothing can handle this" message. `opts.noTools` empties the
tool list (for a narration-only turn); `opts.autoConfirm` bypasses the interactive
confirm gate (for unattended/scheduled runs).

**Neutral conversation** (`conversation.js`) — one transcript format every adapter
translates to/from, so switching models mid-conversation doesn't lose context. A
message's `raw: {adapter, content}` carries a model's own reply object verbatim when
needed — Gemini's `thought_signature` (see Gotchas) is why this exists.

## Chat Persistence (`server/db.js`, `server/chat-store.js`)

Conversations survive a restart and are browsable from a Chat History screen. **SQLite
via Node 24's built-in `node:sqlite`**, not the project's usual `data/*.json` pattern —
the one deliberate exception. Chosen over JSON because conversations are append-heavy
and searched: a JSON file means rewriting the whole conversation on every message and
linear-scanning every file to search, where SQLite gives real full-text search (FTS5)
and atomic transactions for free, confirmed to need zero install on this Node version
before committing to the design. `db.js` is the only module that touches `DatabaseSync`
directly — everything else goes through `chat-store.js` (or Memory's own store, below).
Migrations are keyed on `PRAGMA user_version`, an ordered array in `db.js` — a later
schema change is just another entry, never a rewrite.

`conversation.js`'s in-memory 60-message window (what a model actually sees, per turn)
is unchanged — that cap was already a context-window limit, never a retention policy.
What's new: `bindSession(sessionId)` marks a session as persistent, and every push to a
bound session also lands in `chat-store.js`, which keeps the **full** transcript
forever. `hydrate(sessionId)` loads a persisted conversation's tail back into that
working window — on startup (resuming whatever was active) and whenever the user opens
an older conversation from Chat History.

**Session id is the active conversation's id now, not the hardcoded string `'main'`.**
`brain.js`'s `getActiveSessionId()` is the one place this is resolved — lazily creates
the first conversation on a fresh install, otherwise resumes chat-store.js's saved
active id. Every call site that used to hardcode `'main'` (server.js's attachment
prep, the monitor 'act' trigger) now calls this instead, so a monitor's follow-up (or
an attachment) always lands in whatever conversation the user actually has open.

## Memory (`server/memory/*.js`)

See `server/memory/CLAUDE.md` (loads automatically when working in that directory) for
the module-by-module breakdown. The decisions that matter beyond that file:

- **Approval-first is the only policy, and it lives behind one seam.**
  `memory-policy.js`'s `decide()` always returns `'require-approval'` in this build —
  every caller asks it, none hardcode the assumption themselves. A future configurable
  trust level is a change to that one function, not a rewrite of the skills/UI that
  currently assume approval-first.
- **Nothing is extracted per turn.** `memory-review.js` batches extraction into one
  model call per *checkpoint* — new chat, Jarvis reopening (checked once, at server
  startup), a scheduled task finishing (only `prompt`-type actions, since those are the
  one action type that can plausibly surface a new personal fact), or the model itself
  calling `checkpoint_memories` when it senses a topic has wrapped up. An explicit
  "remember that X" never goes through this at all — `remember_about_me` files it
  directly at the moment the user confirms, since that confirmation already **is** the
  approval a checkpoint exists to obtain.
- **Every checkpoint call site is fire-and-forget.** A checkpoint must never delay the
  user's own reply or make "New chat" feel slow — see `brain.js`'s `resetConversation()`
  and `checkpoint_memories.js` for the pattern (`.catch(err => console.error(...))`,
  never `await`ed by the caller that triggers it).
- **Conversations and memories never cascade into each other, enforced by the schema,
  not by remembering it.** `memory_candidates.conversation_id` has `ON DELETE CASCADE` —
  an unreviewed draft dies with the conversation it came from. `memories` has **no**
  foreign key to `conversations` at all — an approved memory cannot structurally be
  cascaded away by a conversation delete, and deleting a memory cannot touch a
  conversation.
- **"Recall" is not a tool.** The full approved-memory set is small enough to sit
  directly in the system prompt (`prompt.js`'s `memorySection()`, injected in
  `systemInstructionFor()`) — nothing is ever searched for at answer time.
- **The old "About You" store is gone.** `data/profile.json` was migrated once (inside
  `db.js`'s migration step, so it can only ever run once) into Memory's `About You`
  category. `profile.js` is now a thin adapter over `memory-store.js` fixed to that one
  category — its exported function shapes never changed, so `server.js`'s `/api/profile`
  routes and `public/screens/profile.js` needed no edits. `remember_about_me` writes
  into that same fixed category directly (never asks the model to pick one on a quick
  voice utterance) — the only place a category is genuinely chosen is inside a
  checkpoint's own extraction call, which has time to reason about it.

## Front-end (public/)

See `public/CLAUDE.md` (loads automatically when working in that directory) for the
voice engines (`PipelineEngine`/`LiveEngine`), the 3D orb, and the SECTIONS-driven
navigation/modal system.

## Skills (`server/skills/*.js`)

Auto-loaded by `skills/index.js` — file anatomy, confirm/meta mechanics, Folder Skills,
and script execution are all in `server/skills/CLAUDE.md` (loads automatically when
working in that directory). The one rule that stays here because it's cross-cutting:

**PERMANENT RULE — Jarvis's built-in abilities (this file's list above) are not Skills
and must NEVER appear as a Skill anywhere in the UI.** This includes, without limit: the
Skills screen's own list, its create/edit forms, any browse/gallery/catalog/marketplace
view, and any other screen's picker that lists "things Jarvis can do" (e.g. a briefing's
data-source picker). A Skill is knowledge Jarvis doesn't already have — a process, a
house style, a template — never a rename of an existing ability. Structurally enforced,
not just remembered — see `server/skills/CLAUDE.md` for how — because this has
regressed multiple times before despite being called out each time (see
`skills-system-rebuild.md` project memory / `handoff-archive.md` for incident history).

## Scheduler + briefing (`server/scheduler/*.js`)

See `server/scheduler/CLAUDE.md` (loads automatically when working in that directory)
for the module-by-module breakdown.

## Computer control (`server/control/*.js`)

Lets Jarvis actually operate the desktop — click, type, read windows, launch apps —
toward a stated goal, via its own loop, independent of `models/runner.js`'s chat loop.
See `server/control/CLAUDE.md` (loads automatically when working in that directory) for
the module-by-module breakdown.

## App Control connectors (`server/connectors/*.js`)

See `server/connectors/CLAUDE.md` (loads automatically when working in that directory)
for the full mechanism breakdown (MCP/API/CLI/browser/files), the standing-permission-
vs-runtime-confirmation design history, the connector detail pages, and the verified
known-service OAuth shortcuts.

## Sandbox (`server/sandbox/`)

Isolated code execution, used by `skills/run_code.js`, `skills/analyze_spreadsheet.js`,
and `skills/run_skill_script.js` (see the "Skills" section above) — a general facility,
not Skills-only. See `server/sandbox/CLAUDE.md` (loads automatically when working in
that directory) for the backend breakdown (`wsl`/`restricted`) and verification status.

## Monitoring (`server/monitor/`)

"Watch for X, then act", driven by `skills/watch_for.js`/`skills/stop_watching.js`. See
`server/monitor/CLAUDE.md` (loads automatically when working in that directory) for the
check-kind and UI details.

## One-off model calls (`server/ai.js`)

A third way to drive a model, alongside `models/runner.js` (chat turns, tools,
persistent transcript) and `control/session.js` (the control loop): **one prompt,
one answer, no tools**. `askModel({prompt, system, media, json, modelId, only,
need, background})` walks the ranked candidates exactly as runner.js does,
calling `adapter.stream()` with a `systemOverride`.

- `json: true` asks for JSON and parses it with `extractJson()` — tolerant of
  ``` fences and trailing prose, so every adapter works with no per-provider
  structured-output support. A model that returns unparseable output is *not*
  marked unhealthy (it's working, just not following format) — the next
  candidate is tried instead.
- **`need: {video, audio, vision, webSearch}` is enforced here, not in
  `models/router.js`.** The router ranks models for *conversation*, where "can
  it watch a video" never comes up. A capability is gated twice: the adapter's
  `CAPABILITIES` ceiling AND the model's own `caps`.
- `only: true` disables fallback — **required whenever `media` holds an
  uploaded file**, since the upload lives against that one model's API key and
  any other provider would get a URI it can't read.
- **`pickModels()`** returns the full ranked candidate list, not just the top
  pick — useful whenever something must be *prepared* before the call (e.g.
  attaching media) and could fail for the top-ranked model specifically while
  the next one down would be fine. `content/investigator.js`'s
  `examineMediaFile()`/`examineYoutube()` walk it so one bad winner doesn't
  end the whole job.
- Safe for skills to import (no path to `skills/index.js` or `runner.js`).

**Capabilities.** Each adapter exports `CAPABILITIES`; `adapters/index.js`'s
`getCapabilities()` reads it, and an adapter that declares nothing is treated as
text-only. `catalog.js`'s `withCapabilityDefaults()` backfills missing flags,
called from `registry.js`'s `hydrate()` — **at read time, so no data migration
was needed** for models saved before these flags existed. Gemini is currently
the only adapter implementing `video`/`audio`/`webSearch`; teaching another one
is a `searchGrounded()` / `uploadFile()` export plus flipping its flag, and
research/analysis pick it up automatically.

## Research (`server/research.js`)

Before this, Jarvis could not look anything up: `web_search` only opens a
browser tab for the human, `read_web_page` needs a URL you already have.

Two backends, cheapest first: the **free path** (search over plain HTTP → fetch
the top pages → one `askModel` call to synthesize with sources attached), then
**model-native search** (`adapter.searchGrounded`, Gemini's Google Search
grounding) only if the free path comes back thin. That order is deliberate on a
free tier, where one Gemini request is 1/20th of a day.

**Search with keywords, not the question.** `toSearchQuery()` reduces a full
question down to keywords before hitting the free search path (preferring a
quoted claim inside the question as the real subject) — a raw question can
return zero results where the same subject reduced to keywords returns dozens.
Callers that already know the concise subject pass `searchQuery` explicitly.

`via` ('web' | 'model-search') is returned and shown to the user — where an
answer came from changes what it's worth.

## Planning Partner (`server/projects/`)

`project-store.js` (leaf CRUD) · `assistants.js` (assistant profiles as data) ·
`project-engine.js` (the engine). **Rebuilt from scratch** — talking through a project
is just conversation, with no fixed `questions[]`/`answers{}` queue. See
`server/projects/CLAUDE.md` (loads automatically when working in that directory) for
the five-function breakdown (`startProject`/`noteDecision`/`researchProject`/
`writePlan`/`writePrompts`, none chaining into another).

## Content Analysis (`server/content/`)

`content-store.js` (leaf CRUD) · `intake.js` (the free glance) · `investigator.js` (the
engine). **Rebuilt from scratch** — sharing something never triggers a model call or a
fixed analysis template; both are opt-in per request. See `server/content/CLAUDE.md`
(loads automatically when working in that directory) for the intake/examine breakdown
and the four tools (`share_content`/`examine_content`/`check_claim`/`look_it_up`).

## Attachments (`server/attachments.js`)

Anything can be attached from the main composer (paperclip, drag-anywhere, or
paste). `prepareForTurn(ids)` decides how each file enters the turn, and the
split is the whole design:

- **Inline** — images and short text documents ride *inside* the message.
  Images stay in the transcript at full fidelity (the deliberate Claude-like
  choice, so "what does line three say?" still works later); text documents
  are pasted in as text, which needs no capability at all and therefore works
  on every model. `media.js`'s `inlineAttachment()` is the default path for
  images — all three adapters accept inline base64 (`gemini.js` `inlineData`,
  `anthropic.js` base64 image source, `openai-compatible.js` `data:` URL), so
  no provider upload API is needed for this. `adapter.uploadFile` (only
  `gemini.js` implements it) is reserved for video, audio, and anything over
  ~3.5MB raw — Anthropic caps an image at 5MB *encoded*, and base64 inflates
  by 4/3. Reach for the Files API last, not first.
- **Registered, never auto-read** — video and audio go through
  `content/investigator.js`'s `share()`: the free glance only, same as a
  shared link. They are NOT read or watched automatically — the note this
  leaves for the model says what the attachment appears to be and to ask
  what's wanted (or call `examine_content` straight away if the user already
  said in the same message). This used to auto-ingest the file the instant
  it arrived; that violated the same "don't analyse before being asked" rule
  `share_content` enforces everywhere else content enters the conversation.

An image turn sets `need.vision`, which `runner.js`'s `buildCandidateList()`
enforces via `ai.js`'s `meetsNeed()` — the filter runs *before* the manual
pick, so even a pinned text-only model is skipped rather than handed bytes it
can't read.

**Word / Excel / PowerPoint** — a third path, inline like text documents, read
into Markdown by `server/documents/`. See `server/documents/CLAUDE.md` (loads
automatically when working in that directory).

An attachment's file-kind classification (`fileKind()` in `media.js`, plus
`isOfficeDocument()` in `documents/index.js`) still only looks at the
extension — anything neither list recognises falls through to a generic
byte-sniff text reader in `attachments.js` (reject on a NUL byte, honour a
UTF-16 BOM, otherwise decode UTF-8 and reject on too high a replacement-
character ratio) rather than a fixed, ever-growing extension list. This is
what makes an arbitrary code/config file (`.js`, `.py`, `.ini`, `.log`, ...)
readable without a dedicated rule for each one.

## Gotchas

- **Gemini model names deprecate fast and docs pages are unreliable** — a doc fetch
  described an "Interactions API" (`ai.interactions.create`) that doesn't exist in the
  installed SDK. When a model/API shape matters, check `node_modules/@google/genai/dist/`
  directly (grep the `.d.ts`/`.mjs`) or hit the live API, not just fetched docs.
- **Free-tier quota varies wildly by model and drifts over time** — current default is
  `gemini-3.5-flash` (`adapters/gemini.js`, `models/catalog.js`); some models are
  retired entirely for new keys. Verify against `ai.models.generateContent` directly if
  quota errors look wrong rather than trusting a remembered number.
- **`thought_signature` must round-trip verbatim** on Gemini tool-calling turns — when
  replaying history, push the model's actual `response.candidates[0].content` back, not
  a hand-rebuilt `{role, parts}` object, or follow-up calls get rejected (400).
- **SDK error `.message` is raw JSON**, not human-readable — e.g. Anthropic's is at
  `err.error.error.message`, OpenAI's at `err.error.message`, Gemini's needs
  `JSON.parse(err.message).error.message`. Every adapter exports a `friendlyError()`
  extracting it — follow that pattern for any new error surface shown directly to the user.
- **Streaming + function calls**: each chunk from `generateContentStream` is an
  incremental delta (not cumulative) — concatenate `chunk.candidates[0].content.parts`
  across all chunks to reconstruct the full turn correctly.
- **Circular-import deadlock, and the exact invariant that avoids it.**
  `skills/index.js` dynamically imports every file in `server/skills/` at load time; if
  any of those files (transitively) imports something that imports `skills/index.js`
  back, dynamic `import()` deadlocks (it waits for the target module to finish
  evaluating, which is exactly what's blocked on this same import resolving). **The
  rule: nothing under `server/skills/` may import `skills/index.js`, `models/runner.js`,
  `scheduler/scheduler.js`, `scheduler/briefing.js`, or `control/session.js` — directly
  or transitively.** This is why `task-store.js`, `briefing-config.js`, and
  `skill-store.js` are split out as dependency-free leaf modules. The direct-import
  exception: a skill file (or `control/session.js`) importing one specific *other* leaf
  skill file directly (e.g. `control/session.js` importing `skills/open_app.js`) is fine
  — the forbidden edge is importing the *loader* or anything that transitively reaches
  it, not an individual leaf skill module.
- **Node's `fetch` blocks a handful of "unsafe" ports** (9, 21, 25, ...) with a `bad
  port` error unrelated to whether anything is listening — don't use those when testing
  discovery/connection-refused error paths; a real unbound high port (e.g. 19999) gives
  the actual `ECONNREFUSED` you want to test against.
- **A refused local connection's real error is buried** — the `openai` SDK wraps it as
  `TypeError: fetch failed` with `.message` reduced to "Connection error."; the actual
  `ECONNREFUSED` is at `err.cause.cause.code`. `adapters/openai-compatible.js`'s
  `friendlyError()` walks the `.cause` chain rather than trusting `.message`.
- **The user's own running instance can restart itself mid-session, independent of
  anything the current Claude session did** — observed twice in one session (port
  3000's PID changed with no kill issued against it), most likely a second concurrent
  session or the user restarting it themselves (see the "second Claude session" note
  above). The symptom is confusing if you don't know to expect it: a route that
  obviously exists in the file on disk `curl`s a 404 against the real port, because
  that process loaded an in-progress snapshot of the code before the edit landed. Don't
  assume your own change is broken — check the real process's actual start time
  (`Get-Process -Id <pid> | Select StartTime`) against the file's mtime before
  concluding anything. It resolves itself the next time the user restarts normally; **never**
  restart it yourself to "fix" this (see the restart-caution note above).
