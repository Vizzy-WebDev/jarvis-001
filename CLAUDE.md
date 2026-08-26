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
  tts/               Provider-agnostic server-side TTS seam (see "TTS provider system")
  live.js            WebSocket proxy (/api/live) to Gemini Live, via ai.live.connect()
  events.js          SSE hub (/api/events) for server -> browser push
  db.js              The one SQLite connection (Chat History + Memory), migrations (see "Chat Persistence")
  chat-store.js      Conversation/message CRUD + full-text search over SQLite (see "Chat Persistence")
  memory/            Memory Manager, policy seam, checkpoint engine (see "Memory")
  profile.js         Thin adapter over Memory's "About You" category — data/profile.json is gone (see "Memory")
  adapters/          One module per wire format (see "Model system")
  models/            Registry, connections, routing, health, execution (see "Model system")
  scheduler/         Tasks, recurrence, briefing (see "Scheduler + briefing")
  jobs/              Background Task Orchestration ("Jobs") — long-running work backgrounded from live
                     conversation, distinct from scheduler/ above (see "Background Task Orchestration")
  tools/             Auto-loaded executable capabilities (below) — get_weather, open_app, run_code, ...
  skills/            Folder Skills ONLY — SKILL.md instructions; skills/store/ holds install logic (see "Skills")
  capabilities.js    The composition seam: tools + folder Skills + connectors -> one declaration list,
                     one invoke(), one confirm gate (see "Skills" and "Tools")
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
                     plus jarvis.db (SQLite): Chat History + Memory + Jobs (see those sections). profile.json
                     is gone, migrated into Memory.
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
`turn-check.js` depends on `GEMINI_API_KEY` regardless of which model is chatting
(TTS no longer does — see "TTS provider system"). **`assumesVision()` (`catalog.js`) does not treat every non-local cloud model
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
confirm gate (for unattended/scheduled runs). **`opts.allowedTools` (an array) is
enforced, not just offered** — a call naming anything outside it is refused before
`invoke()` ever runs, and the same opt pre-unlocks the names it lists regardless of
`core` status; see "Background Task Orchestration"'s note on this for why (found via a
Jobs test, but the fix is general — it also closes the same gap for `scheduler.js`'s own
per-task connector restriction).

**Neutral conversation** (`conversation.js`) — one transcript format every adapter
translates to/from, so switching models mid-conversation doesn't lose context. A
message's `raw: {adapter, content}` carries a model's own reply object verbatim when
needed — Gemini's `thought_signature` (see Gotchas) is why this exists.

## Voice-layer providers: generic keys, STT, TTS

Three small, deliberately layered pieces, none of which know about the others' provider
names — the point of the design is that a real provider is "one adapter file + one
registry entry," never a change to storage or UI:

- **`server/external-services.js`** — generic, user-named external-service key storage.
  A "service" is any name the user types (Deepgram, ElevenLabs, ...) plus a key and an
  optional second field (e.g. a Voice ID); `ref` is a stable slug derived from the typed
  label. Structural metadata (`label`, `extraFieldLabel`) lives in
  `data/external-services.json` (`store.js`'s `readJson`/`writeJson`); every actual
  credential value goes through `config.js`'s existing `getSecret`/`saveSecret`/
  `deleteSecret` unchanged — `${ref}` for the key, `${ref}_extra` for the second field's
  value. **This is not a general-purpose secret store** — model-provider connection
  secrets stay entirely on `models/registry.js`'s own routes, never here, so a bug here
  can't corrupt a model connection's key. `server.js`'s `/api/external-services*` routes
  are the only consumer of `addOrUpdateService`/`removeServiceKey`/`deleteService`
  (Remove clears the key and keeps the row; a separate "delete this service" removes the
  row entirely) — a real adapter's own module (see below) reads a saved key/field via
  `getKey(ref)`/`getExtraField(ref)`.
- **`server/stt/`** — real-time speech-to-text. `stt/deepgram.js` is the one real
  provider today (its ref is created once, deterministically, via a one-time migration
  of the legacy `JARVIS_SECRET_DEEPGRAM`, so `server.js`'s `EXTERNAL_SERVICE_TESTERS` map
  can key its live-test function by that exact stable ref). The browser's own
  `SpeechRecognition` is the automatic no-key fallback (`server/duplex.js` reports
  `mode:'browser'`) and has no server component at all.
- **`server/tts/`** — server-side text-to-speech, `stream(text, {voice}) -> async
  generator of {buffer, mimeType}` on every provider (even a non-streaming one, which
  yields exactly one chunk). Unlike Deepgram, **a TTS provider's ref is NOT stable** —
  it's whatever the user typed as a service label, slugified, so `tts/index.js` never
  matches a provider to a service by exact ref string. Instead every adapter (currently
  just `tts/elevenlabs.js`) exports `matchesRef(ref)`, a small, self-contained, bounded
  recognizer (normalizes and checks for its own provider name, so "ElevenLabs",
  "Elevenlab", "11labs" etc. all resolve to the same provider) — `tts/index.js` just asks
  each registered adapter "is this configured service yours?" and defers entirely to the
  answer, so the shared seam itself never hardcodes a provider's name anywhere.
  `tts/index.js`'s `testerFor(ref)` gives `server.js`'s generic test route the same
  dynamic dispatch for TTS providers that `EXTERNAL_SERVICE_TESTERS` gives Deepgram.
  Voice selection has no separate picker UI at all — a provider resolves its own voice
  server-side, from the service's optional extra field if set, otherwise a sensible
  provider-specific default (`tts/elevenlabs.js`'s: the account's own first available
  voice from a live `GET /v1/voices` call, briefly cached — **not** a single hardcoded
  "standard" voice id, confirmed live that a free-tier account can 400 on one of those).
  The free, offline `browser` voice (`public/browser-speaker.js`) is deliberately not a
  provider here at all — no server component, handled entirely client-side, and is the
  one guaranteed-always-available `voiceOutput` value both voice engines default to.

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

- **Approval is tiered behind one seam, and one hard floor never moves.**
  `memory-policy.js`'s `decide(candidate, {trust})` is a pure function — every caller
  asks it, none hardcode "ask the user" or "just save it" themselves. `prefs.js`'s
  `memoryTrust` (`'ask'` | `'balanced'` | `'auto'`, default `'ask'` — reproduces the
  original approval-first behavior byte for byte for anyone who never opens the Memory
  screen) picks a confidence threshold the extraction model's own per-candidate score
  must clear to auto-save. **A candidate that conflicts with an existing memory always
  requires approval, at every trust level, with no override** — resolving a conflict
  changes or duplicates something that already exists, and that is never done silently.
  A `memories` row's `origin` column (`'approved'` | `'auto'` | `'explicit'` | `'legacy'`)
  records how consent was given, separately from `source_kind` (where the content came
  from) — the Memory screen's badge reads directly off it.
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
- **Recalling a durable FACT is not a tool** — the full approved-memory set is small
  enough to sit directly in the system prompt (`prompt.js`'s `memorySection()`, injected
  in `systemInstructionFor()`), so nothing is ever searched for to answer "what do you
  know about me." Curation (small, approved, or at least confidence-gated) is what keeps
  this affordable — a store that grew without limit would force search here too.
  **Recalling something SAID is a different problem and does use a tool** —
  `search_conversations` (`server/tools/search_conversations.js`) full-text-searches
  every past conversation via `chat-store.js`'s `searchMessages()` (built on the same
  `messages_fts` index Chat History's own search already used, just not previously
  reachable from a live turn). Dated results are the point: a past statement is not
  automatically still true, and the model is instructed to say when something was said
  rather than assert it as current.
- **The old "About You" store is gone.** `data/profile.json` was migrated once (inside
  `db.js`'s migration step, so it can only ever run once) into Memory's `About You`
  category. `profile.js` is now a thin adapter over `memory-store.js` fixed to that one
  category — its exported function shapes never changed, so `server.js`'s `/api/profile`
  routes and `public/screens/profile.js` needed no edits. `remember_about_me` writes
  into that same fixed category directly (never asks the model to pick one on a quick
  voice utterance) — the only place a category is genuinely chosen is inside a
  checkpoint's own extraction call, which has time to reason about it.
- **The Memory screen (`public/screens/memory.js`, `#/memory`) is `memory-store.js`'s
  first UI.** The engine (browse, search, edit, merge, archive, version history) has been
  complete since Memory shipped, deliberately screenless while every save was
  individually approved. Once a save can happen without being asked, seeing and undoing
  it stops being optional — the trust dial, and every memory with its origin badge, live
  here.

## Front-end (public/)

See `public/CLAUDE.md` (loads automatically when working in that directory) for the
voice engines (`PipelineEngine`/`LiveEngine`), the 3D orb, and the SECTIONS-driven
navigation/modal system.

## Tools (`server/tools/*.js`)

Real executable capabilities — `get_weather`, `open_app`, `run_code`, `schedule_task`,
...  Auto-loaded by `tools/index.js`; file anatomy, confirm/meta mechanics, and the
OS-command allowlist discipline are all in `server/tools/CLAUDE.md` (loads automatically
when working in that directory).

## Skills (`server/skills/*.js`)

**A Skill is a folder of instructions, never a rename of an executable capability.**
This directory used to hold both — built-in tools AND folder Skills — under the one word
"skill," which is what let a real capability get offered as an installable Skill in the
UI three separate times before this split. It now holds ONLY folder Skills:
`SKILL.md`-based instructions under `data/skills/<name>/`. File anatomy, the folder-Skill
mechanics, and upload/replace/download are all in `server/skills/CLAUDE.md` (loads
automatically when working in that directory). The one rule that stays here because it's
cross-cutting:

**PERMANENT RULE — Jarvis's built-in tools (`server/tools/`, listed in the Structure
block above) are not Skills and must NEVER appear as a Skill anywhere in the UI.** This
includes, without limit: the Skills screen's own list, its create/edit forms, any
browse/gallery/catalog/marketplace view, and any other screen's picker that lists
"things Jarvis can do" (e.g. a briefing's data-source picker). A Skill is knowledge
Jarvis doesn't already have — a process, a house style, a template — never a rename of
an existing ability. Structurally enforced, not just remembered — a Skills UI may only
ever read `listUserSkills()` (`server/skills/store/skill-files.js`), which has no code
path back to a built-in tool; see `server/skills/CLAUDE.md` for how — because this has
regressed multiple times before despite being called out each time (see
`skills-system-rebuild.md` project memory / `handoff-archive.md` for incident history).

## `server/capabilities.js` — the composition seam

The one place tools (`server/tools/`), folder Skills (`server/skills/`), and connector
tools (`server/connectors/`) merge into what a model, a scheduled task, or a briefing
source actually sees: `getToolDeclarations()` (model-facing, stripped to
`{name, description, parameters}`), `listCapabilities()` (the tagged union — a Skills UI
must never read this, see the PERMANENT RULE above), `listStepCandidates()` (built-in
tools only, non-meta — the one honest enumeration a pipeline step picker can be built
on), `hasCapability()`, and `invoke()` (the one dispatcher; owns the confirm-and-
read-back token gate, moved here verbatim from the old merged `skills/index.js`).
**A third confirm mode** lives in the same gate, alongside interactive read-back and
`ctx.autoConfirm`: `ctx.onEscalate` (present only for a background Job's own turn — see
"Background Task Orchestration") parks the decision instead of minting a token nobody
will resend. `listCapabilities()`/`listStepCandidates()` also exclude any tool marked
`internal: true` (`server/tools/CLAUDE.md`) — a different axis from `meta`, for a tool
that must still reach a `background:true` turn (which strips `meta` tools before
`allowedTools` is even considered) but has no business appearing in a task/briefing
picker.

## Scheduler + briefing (`server/scheduler/*.js`)

See `server/scheduler/CLAUDE.md` (loads automatically when working in that directory)
for the module-by-module breakdown.

## Background Task Orchestration (Jobs) (`server/jobs/*.js`)

Lets Jarvis work on something long-running in the background while the user keeps
talking about anything else — a *different* mechanism from the scheduler above: a
scheduled task runs on a clock Jarvis has no judgment about; a Job is work Jarvis (its
own judgment) or the user ("keep working on that in the background") chose to
background right now. See `server/jobs/CLAUDE.md` (loads automatically when working in
that directory) for the module-by-module breakdown. The decisions that matter beyond
that file:

- **A layered chain, not one generic task-runner.** The Conversation Manager (the live
  chat turn) is the only thing the user ever talks to — it decides *whether* to
  background something and does all the talking, via three tools:
  `work_in_background` (one admission model call decides a title/`kind`/plan, then
  creates the job — capacity is checked BEFORE that call is spent, so a full owner
  doesn't cost a wasted one), `check_on_work` (Tier 3 pull, plus the one way a live
  conversation resolves a job parked `awaiting_decision`: `respond:'keep_going'` with
  optional guidance), and `stop_working_on` (cancel, any status). The Orchestrator
  (`server/jobs/orchestrator.js`) decides *how* the work actually gets done —
  admission/capacity/resource arbitration, supervision, one automatic retry before
  escalating. A Worker (`server/jobs/worker.js`) runs on its OWN session
  (`` `job:${id}` ``) — structurally, not by convention, a worker cannot write into the
  conversation the owner is looking at: `conversation.js` keys sessions in a `Map` and
  `runner.js` re-reads a session's messages fresh every step, and a worker session is
  never `bindSession()`'d, so it never appears in Chat History either.
- **A write-ahead trace is what makes crash recovery honest instead of declared.** Every
  effectful action gets an `intent` row (`job_trace`) BEFORE it runs and an `outcome`
  row after — a crash between the two still leaves the intent's `effect` on record.
  `classifyRecovery()` (`job-policy.js`) derives `resumable` / `restartable` /
  `needs_input` / `unrecoverable` from that trace alone: ANY `effect:'external'` row
  (even a completed one, even a dangling intent) is `unrecoverable`, since restarting
  risks repeating something that can't be safely repeated — the pessimistic default at
  every branch, on purpose. A `computer`-kind job is therefore never `resumable` after a
  real crash (operating the real desktop is always `external`) — a fact the trace
  surfaces honestly rather than one any code asserts.
- **Heartbeat silence and a live semantic stall are different failure modes, caught two
  different ways.** `isHung()` (heartbeat older than `HANG_TIMEOUT_MS`) catches a
  genuinely dead/hung process. `diagnoseStall()` catches a worker that's alive, still
  emitting events, going nowhere: exact tool-call repetition, a 2/3-cycle oscillation,
  repeated failure, or near-duplicate reasoning text with no tool calls at all (the one
  signal that needs no tool call to fire). Either one spends the job's single automatic
  retry (a corrective nudge pushed into the SAME live session — nothing about pausing
  and resuming discards its context) before escalating to a Tier 1 outbox row.
- **A third confirm mode, alongside interactive read-back and `ctx.autoConfirm`.** A
  background job is neither interactive (nobody is present for a live read-back) nor
  pre-consented (unlike a scheduled task, nobody agreed to this specific action up
  front). `ctx.onEscalate` (`capabilities.js`'s `invoke()`, reached only via
  `runner.js`'s `opts.onEscalate` passthrough — present only for a Job's own turn) parks
  the job into `awaiting_decision` and raises a Tier 1 outbox row instead of minting a
  `confirm_token` nobody will resend — a background job may wait hours, far past
  `CONFIRM_TTL_MS`. The durable record of consent is the outbox row and the job's own
  status, not a short-lived token.
- **`allowedTools` is now ENFORCED at invocation time, not just offered at declaration
  time — a real fix, not a Jobs-only one.** Before this, `runner.js`'s `toolsForTurn`
  only decided which tool DECLARATIONS a model saw; `invoke()` had no idea an allowlist
  existed and would run a call naming anything else. A real model can't call a tool it
  was never given a schema for, but a hallucinated or malformed call could — confirmed
  live, not assumed: a `research`-kind job's own test call still reached `get_time` even
  though it was never in that kind's tool list. `runOnEntry` now refuses (with a plain
  `{ok:false}` result) any call whose name isn't in `opts.allowedTools` when that opt is
  an array — this also closes the same latent gap for `scheduler.js`'s own per-task
  connector restriction, which relied on the exact same, previously-unenforced opt.
  Relatedly, `opts.allowedTools` now also PRE-UNLOCKS the names it lists (seeds
  `runOnEntry`'s own `unlocked` Set) — an explicit allowlist naming a non-core tool used
  to have no effect at all, since visibility still fell back to core-only.
- **Interruption tiers are structural, not timer-based, by construction.** Tier 1/2
  decisions are delivered via `prompt.js`'s `jobsSection()` — injected into the system
  prompt of a turn the user ALREADY started (never proactively pushed), and gated on
  `!opts.background` so it never leaks into a scheduled task's or a Job's OWN turn (both
  set `background:true` for exactly this reason — `systemInstructionFor()` needed that
  opt threaded one hop further, into the object `runner.js` hands `adapter.stream()`,
  which it previously wasn't). Not marked "delivered" the moment it's shown — only the
  action that actually resolves the decision (`resumeStuckJob`/`cancelJob`/
  `resumeOrphan`/`restartOrphan`) marks its outbox row delivered, so a turn that fails
  before the model ever replies loses nothing. Tier 3 is pull-only
  (`check_on_work`) plus the existing ambient notification channel — never surfaced
  proactively at all.
- **Splitting is capped at depth 2, structurally.** A Worker's `request_job_split` call
  is judged by the Orchestrator (one model call, denying on any real uncertainty), never
  approved automatically. Every approved piece's `parent_id` is the ROOT ancestor,
  resolved in one hop (`job.parentId || job.id`) — so a level-2 piece requesting a
  FURTHER split creates a PEER under the same root, never a child of itself. Confirmed
  live: a directly-planted level-2 job's own split request produced grandchildren whose
  `parent_id` was the true root, not the requesting job.
- **`kind` is a tool-list restriction, nothing more — worth being explicit about, since
  the name invites a stronger reading than the code delivers.** `generic` isn't "no
  expertise" — it's the literal opposite, the FULL unrestricted tool catalog with no
  fence at all. `research`/`files` are small hardcoded tool-name arrays chosen once, at
  build time. There is no per-kind system prompt, no role or expertise framing, and —
  discovered while explaining this to the user, not designed this way on purpose — the
  admission call's own `plan.summary`/`plan.steps` are computed, shown in the UI, and
  never actually fed to the worker's own prompt at all. A split's pieces are always
  created `kind:'generic'` (skipping a second admission call per piece to keep a split's
  cost at one judgment call total), so two pieces needing genuinely different expertise
  get the identical unrestricted toolkit and the identical generic instructions today.
  **Whether to build a genuinely adaptable worker — the Orchestrator selecting tools AND
  framing per task, not a fixed kind enum — is an open, undecided design question**, not
  something the current `generic` kind already does under a different name.
- **`kind:'computer'` never starts unattended.** Autonomously operating the real desktop
  is exactly the kind of outward-facing, hard-to-undo action the build spec says must
  come to the owner — so a fresh `computer` job is parked straight into
  `awaiting_decision` with a Tier 1 "OK to start?" row rather than `queued`, and
  `resumeStuckJob` (the SAME "keep going" mechanism every other parked decision uses,
  no separate confirm-token machinery) is the only thing that ever actually starts it.
  Confirmed live: a `computer`-kind job sat inert with `startedAt: null` across multiple
  supervisor ticks until explicitly resumed — it was never once allowed to auto-start.

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
- Safe for tools to import (no path to `tools/index.js`, `capabilities.js`, or `runner.js`).

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
  `tools/index.js` dynamically imports every file in `server/tools/` at load time; if
  any of those files (transitively) imports something that imports `tools/index.js`
  back, dynamic `import()` deadlocks (it waits for the target module to finish
  evaluating, which is exactly what's blocked on this same import resolving). **The
  rule: nothing under `server/tools/` may import `tools/index.js`, `capabilities.js`,
  `models/runner.js`, `scheduler/scheduler.js`, `scheduler/briefing.js`, or
  `control/session.js` — directly or transitively.** This is why `task-store.js`,
  `briefing-config.js`, and `skill-files.js` are split out as dependency-free leaf
  modules. The direct-import exception: a tool file (or `control/session.js`) importing
  one specific *other* leaf tool file directly (e.g. `control/session.js` importing
  `tools/open_app.js`) is fine — the forbidden edge is importing the *loader* (or
  `capabilities.js`, the seam built on top of it) or anything that transitively reaches
  either, not an individual leaf tool module. A tool file that needs something only
  `capabilities.js` can provide (the reserved-name set, for `create_skill.js`) receives
  it via `ctx`, injected by `capabilities.js`'s `invoke()`, never via a top-level import.
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
