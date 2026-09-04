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
- **To verify what a model actually DID, not what it said it did, read the real
  `toolCalls`/`toolResults` payloads straight out of the `messages` table** (`db.js`,
  read-only, via `node:sqlite`'s `DatabaseSync(path, {readOnly:true})` against
  `data/jarvis.db` directly) — a model's own spoken/typed account of an action succeeding
  or failing is not reliable evidence on its own. Confirmed live: a user's own paraphrase
  of an exchange read exactly like a broken confirmation loop; the real stored payload
  showed the tool had actually been called twice, with a real token, and had genuinely
  succeeded — a completely different (and more serious) bug than the paraphrase
  suggested. A `bindSession()`'d conversation persists every tool call/result verbatim in
  `payload`, so this is always available for anything that happened in a real, saved
  conversation — check this before trusting either the model's own narration or a user's
  summary of it when diagnosing a tool-execution bug.

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

**A command-prefix env assignment (`VAR=val cmd`) is NOT available as `$VAR` inside
that same command line's other arguments — confirmed live, the exact way it broke a
real pre-merge validation run.** `SCRATCH_DATA="<path>" node -e "process.env.JARVIS_DATA_DIR
= '$SCRATCH_DATA';"` looks like it should work but doesn't: `$SCRATCH_DATA` inside the
`-e` string is expanded by the **outer shell**, before the prefix assignment is ever
applied to the child process — and since `SCRATCH_DATA` was never a real shell
variable (only a prefix meant for `node`'s own environment), it silently expands to
an empty string. The result: `process.env.JARVIS_DATA_DIR = '';`, falsy, so
`dataDir()` correctly — and dangerously — fell back to the real project `data/`
directory exactly as designed, writing real test output into the user's actual data.
This is different from setting `PORT`/`JARVIS_DATA_DIR` as real prefix env vars
directly on `node server/server.js` itself (that works fine — the child reads its
own `process.env` directly, no shell-side string interpolation involved). **The
rule: never reference a bash variable via `$VAR` inside a `node -e` string when that
variable was only ever a command-prefix assignment on the same line — either give it
a real `export`/separate assignment first, or just write the literal path directly
into the `-e` string.** Always `ls`/verify the actual scratch directory a write
script claims to have written to, rather than trusting the script's own success
output — that's what caught this one before it went further.

## Pre-merge validation gate

The closest thing this project has to a CI gate — run this (against a scratch
instance, never the user's real port/data) before merging any branch into `main`,
and report each result plainly rather than summarizing as "passed":

1. **Syntax sweep** — `node --check` across every `.js` under `server/` and
   `public/`, not just changed files (a concurrent session's edits count too). Zero
   failures required.
2. **Fresh-install boot** — start the real `server/server.js` in the background
   (`run_in_background: true`, see above) with `JARVIS_DATA_DIR`/`JARVIS_ENV_PATH`
   pointed at empty scratch paths and an unusual `PORT`. Must print `Jarvis is
   running at http://127.0.0.1:<port>` with no unhandled exception.
3. **Migrations + tool loader** — read the scratch `jarvis.db` read-only and confirm
   `PRAGMA user_version` reached the current length of `db.js`'s `MIGRATIONS` array;
   confirm the tool loader's startup log lists every file under `server/tools/`
   (proves nothing new tripped the circular-import deadlock invariant above).
4. **Route smoke test** — `curl` a real GET route (200), a route that takes an id
   with a nonexistent one (a clean 404, not a crash).
5. **Any recently-fixed security behavior** — re-confirm it live rather than by
   reading the code, e.g. a served-content route's forced-download headers on a
   plain request with no special query param.

Teardown: stop the scratch server by the PID actually bound to the scratch port
(`Get-NetTCPConnection -LocalPort <port>`, not a guess), delete the scratch
directory, and confirm the user's real instance/port is unaffected.

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
  heartbeat/         Heartbeat + Trigger + Proactive Attention — Jarvis noticing things on its own,
                     independent of any open conversation, and speaking up first when genuinely warranted.
                     Generalizes Jobs' own Tier 1/2/3 Interruption Broker rather than duplicating it (see
                     "Heartbeat"). Named for the user's own term; unrelated to jobs/job-store.js's
                     `heartbeat_at` (worker-liveness tracking for one running job).
  improvement/       Self-Improvement — Jarvis reviewing its own work and applying what it learns to
                     itself, distinct from memory/ above (facts about the USER) (see "Self-Improvement")
  ops/               Operational Awareness — self-diagnosis + self-heal, environment/system-load
                     awareness, mechanical + semantic Verification (wired at artifact creation, Job
                     completion, and scheduled-task outcomes — NOT yet consequential chat answers, a
                     deliberate cost-scope decision), the generic activity trace both this and Jobs
                     write into (see "Operational Awareness" and server/ops/CLAUDE.md's own status note)
  cost/              Cost tracking — automatic spend/usage tracking across every paid service, plus
                     feeding a real measured cost back into model routing (see "Operational Awareness"
                     and server/cost/CLAUDE.md)
  artifacts/         Output/Artifact generation — real files (docx/xlsx/pptx assembled for real,
                     everything else written as given — pptx carries a narrower verification
                     confidence than docx/xlsx, see its own CLAUDE.md), mechanically verified the
                     instant they're created, served at GET /api/artifacts/:id with a real fixed
                     stored-XSS finding behind it (see "Operational Awareness" and
                     server/artifacts/CLAUDE.md)
  self/              Self-Model — Jarvis's own grounded, evidence-backed self-knowledge: what it is,
                     what it can/can't actually do, what it's doing now and why, what's its own call
                     to make. Reads Memory/Jobs/Self-Improvement/Personality rather than duplicating
                     them (see "Self-Model")
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
                     plus jarvis.db (SQLite): Chat History + Memory + Jobs + Self-Improvement + Self-Model
                     (see those sections). profile.json is gone, migrated into Memory.
```

## Model system (server/adapters/ + server/models/)

Three layers, each with one job:

**Adapters** (`server/adapters/{gemini,anthropic,openai-compatible}.js`) — one per wire
format, not per model. `openai-compatible.js` alone covers OpenAI, Ollama, LM Studio,
OpenRouter, Groq — anything OpenAI-shaped — by pointing `baseUrl` elsewhere. Each exports
`stream(entry, messages, opts)` (async generator over the neutral message format below),
`testConnection(entry)`, `listModels(entry)` (throws on failure — registry.js's
`discoverModels()` catches it and turns it into `{models, error}`), `friendlyError(err)`.
All three now accept `entry.baseUrl` (Anthropic/Gemini included, via each SDK's own
custom-base-URL option) so a Custom connection resolved to any of the three shapes can
actually be called, not just discovered.

**Providers vs. adapters — a provider is what the user picks, an adapter is the wire
format underneath it, and these are deliberately two different things now.**
`server/models/providers.js` (data-only, zero imports) is the five-tile catalog the
"Add a model" screen shows: OpenAI, Anthropic, Gemini, Local server, Custom — never an
adapter name like `openai-compatible`, which used to leak straight into the UI as
`"OpenAI-compatible (OpenAI, Ollama, LM Studio, OpenRouter, Groq, ...)"`. OpenRouter,
Groq, Together, OmniRoute, and any other gateway all go through **Custom**, which has no
fixed adapter of its own — `server/models/probe.js`'s `probeEndpoint()` resolves one by
actually trying the address: normalizes the URL (auto-tries `+ '/v1'`), attempts the
OpenAI chat-completions shape, then Anthropic's, then Gemini's, and returns `steps[]` —
what it tried, in plain language — alongside the result, shown in the UI on both success
and failure rather than the old single "That connection didn't work." A 401 with no key
supplied is classified as "reached it, needs a key," never as "that key is invalid" (the
literal OmniRoute-connection failure this whole redesign started from — no field to type
an address into, then a misleading auth error once one was added by hand).

**A connection stores `provider`, `kind` (`'first-party'|'gateway'|'local'`), and
`keyRequired` as facts, captured once at add time — this is the one thing that made
removing the OpenRouter/gateway tiles safe.** Before this, `catalog.js` re-derived
"is this local? is this an aggregator? does this need a key?" from `adapter` + a
host-regex on every read — workable when every non-first-party connection was
`openai-compatible` with a real address to regex-match, but Custom's whole point is that
the address alone doesn't say what's on the other end. `isLocalConnection()`/
`isAggregatorConnection()` (`catalog.js`) now take an optional `kind` and use it outright
when present, falling back to the original regex only for a connection saved before this
existed (`providers.js`'s `providerForLegacy()` backfills `provider`/`kind` for those at
*read* time — `registry.js`'s `hydrate()` and `server.js`'s `publicConnection()` — never
a data migration; confirmed live that the two pre-existing OpenRouter connections still
classify as `kind:'gateway'` with no changes to `data/connections.json`).
`registry.js`'s `isReady()` and `adapters/openai-compatible.js`'s `requireKeyIfNeeded()`
both check `entry.keyRequired` first (a real boolean overrides the old "any non-openai.com
baseUrl is assumed keyless" guess entirely) before falling back to the same regex.
`server/models/redact.js`'s `redactSecrets()` scrubs a submitted key out of the raw
adapter error text (`registry.js`'s `detail` field on a failed test/discovery, surfaced
in the UI as a collapsible "Technical details" line) before it ever leaves the server.

**Connections + models** (`server/models/connections.js` + `registry.js`) — a
"connection" is one saved address+key (`data/connections.json`); a "model" is one model
name under a connection (`data/models.json`, holding only `connectionId` plus its own
label/caps/tier — `registry.listModels()`/`getModel()` hydrate in the connection's
adapter/baseUrl/secretRef/keyRequired at read time). Several models discovered together
share one connection instead of each duplicating the same key. `updateModel()`
whitelists its patch keys on purpose — never let adapter/baseUrl/secretRef/connectionId
be set through it, or a model desyncs from its connection. Legacy secrets (the original
`gemini`/`anthropic`/`openai` refs) are never deleted by a connection removal —
`turn-check.js` depends on `GEMINI_API_KEY` regardless of which model is chatting
(TTS no longer does — see "TTS provider system"). **`assumesVision()` (`catalog.js`) does not treat every non-local cloud model
as vision-capable** — a stored `kind:'gateway'` (or, absent that, the original
aggregator-host regex) is matched by name-hint only, same as local models, since
discovery-guessed quality scores are unreliable signals for what a model can actually see.

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
mechanics, and upload/replace/download — plus installing straight from a public GitHub
repository link, the "install from a repository or marketplace" path — are all in
`server/skills/CLAUDE.md` (loads automatically when working in that directory). The one
rule that stays here because it's cross-cutting:

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

**Every installed Skill is declared to every turn, and the system prompt tells the model
to actually prefer one, not just discover it.** `prompt.js`'s `skillsSection()` — already
unconditionally injected into the shared system prompt every adapter builds from, live
chat and background alike — lists each enabled Skill by name/description AND instructs:
when what's being asked genuinely matches one, call it and follow it rather than
reasoning the task out from scratch. This closed a confirmed, live gap: asked "how many
Skills do you have," the model used to answer from vague self-conception, folding in
built-in abilities and connected apps under the same word — the honest count was sitting
in the very system prompt the whole time. The same section now also tells it not to make
that conflation when asked directly.

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

**A confirm token must be refused if redeemed in the SAME turn that minted it — enforced
structurally, never left to a prompt instruction alone, and never overridable by the
user's own explicit "skip confirming" request.** Found live in real production testing,
not hypothetical, while testing the Self-Model subsystem below: giving a confirm-gated
tool a real, schema-visible `confirm_token` argument (`getToolDeclarations()`'s
`withConfirmToken()` — see the Gotchas entry on why that fix was needed at all) let a
model, told by the user to skip asking, mint the token and immediately resend it in the
SAME turn — completing an entire ask-and-answer round trip with zero real human reply in
between. `models/runner.js`'s `runTurn()` now mints one `turnId` per call (stable across
every step and candidate-model retry of that one turn, threaded into `invoke()`'s `ctx`
exactly like `sessionId`); `consumePendingToken()` refuses a token whose `mintedTurnId`
matches the redeeming call's own `ctx.turnId` — exactly as if the token were invalid, so
the model just gets asked again, and only a genuinely later turn (a real new message)
can complete it. Only enforced when both sides carry a real `turnId`; a caller outside
the per-turn system (unattended `autoConfirm`/`onEscalate` callers never reach this code
path at all) is unaffected. **The general standard this sets for any future confirm-gated
action:** the user's own explicit, in-the-moment instruction to skip a confirmation must
never be honored for anything effectful — a confirmation is a floor no instruction, even
a direct one from the user, can lower. See `server/tools/CLAUDE.md`'s "Voice-clarity
confirmation" section for the full investigation.

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

## Heartbeat + Trigger + Proactive Attention (`server/heartbeat/*.js`)

Jarvis noticing things on its own — independent of any open conversation — and, when
genuinely warranted, speaking up first. A different mechanism from the Scheduler (a task
runs on a clock Jarvis has no judgment about) and from Jobs' own supervision (an
orchestrator watching a worker it started) — this watches for CONDITIONS worth surfacing
and decides whether they're worth interrupting for. See `server/heartbeat/CLAUDE.md`
for the module-by-module breakdown; the decisions that matter beyond that file:

- **Generic by construction, not by convention.** `sources/registry.js` is pure and zero-
  import — the entire plug-in surface is `registerSource({id, defaultIntervalMs,
  listItems(), check(itemKey)})`. The engine that ticks and the decision layer that
  judges urgency know nothing about what a source actually watches. Day one plugs in
  exactly two: `sources/jobs-source.js` (background Job status) and
  `sources/commitments-source.js` (time-sensitive Memory commitments) — a third source
  later is one `registerSource()` call, nothing about `engine.js`/`decision.js` changes.
- **Frequency is per-item, persisted, and restart-safe — not a hoped-for property, a
  real schema.** `heartbeat_schedule` (db.js migration 13) stores each item's own
  `next_due_at`; a restart never resets it to zero and never re-checks everything at
  once. `engine.js`'s tick processes whatever's due SEQUENTIALLY, capped per tick — the
  cap plus sequential ordering is what turns a big catch-up backlog into several ticks
  of steady work instead of one burst. Confirmed live: 25 items seeded simultaneously
  split cleanly across two ticks (18 then 7) with none double-processed. A `running`
  flag (cleared once, at startup only — `resetStaleRunning()`) prevents a still-running
  check from being started a second time; a thrown error from a source's own
  `listItems()`/`check()` is caught per-source/per-item and never stops another item's
  processing in the same tick — confirmed live with a deliberately broken source
  alongside a healthy one in the same tick.
- **The Interruption Broker is the SAME one Jobs already built, genuinely reused, not a
  parallel mechanism wearing a different name.** Jobs' original `job_outbox` table was
  schema-bound to jobs (`job_id TEXT NOT NULL`); db.js migration 13 rebuilds it as a
  generalized `outbox` table (`server/heartbeat/outbox-store.js`) with `source`/
  `source_ref` alongside a still-real, still-cascading `job_id` column — every one of
  Jobs' own existing call sites (`job-actions.js`, `worker.js`, `orchestrator.js`,
  `server.js`'s job routes) needed zero changes, since `jobs/job-store.js`'s own outbox
  functions became thin wrappers over the generalized store. Confirmed live: migrating a
  real copy of the user's actual database preserved all 12 real `job_outbox` rows intact
  under the new schema. `prompt.js`'s drain (kept under its original name, `jobsSection()`,
  to avoid rippling a rename across every comment referencing it) now words a
  `source:'heartbeat'` row differently and points it at a new tool, `acknowledge_notice`
  — the one resolving action such a row needs that Jobs' own `check_on_work`/
  `stop_working_on` don't apply to.
- **A real, live-caught dedup bug, worth understanding before touching a source's
  `check()`.** A Tier 3 verdict never creates an outbox row at all (nothing to
  interrupt for), so the broker's own undelivered-row dedup has nothing to check
  against for Tier 3 findings. Confirmed live: the first version of `jobs-source.js` had
  no dedup of its own, and a routine, correctly-Tier-3-judged job permission ask
  re-notified — with a fresh spent urgency-decision model call each time — every ~3
  minutes, forever. The fix: a source whose underlying condition can stay true across
  many ticks must track its OWN "already reported" memory via `heartbeat_schedule`'s
  `check_state` column (returning `{finding, checkState}` from `check()`, read back via
  `schedule-store.js`'s `getItem()`) — the same discipline `commitments-source.js`
  already used for its own approaching/overdue flags. Verified live after the fix:
  notification count held flat across multiple further tick cycles for the same
  still-unresolved job, where it had climbed by one every cycle before.
- **One urgency-reasoning step, used only for Heartbeat/Trigger findings — Jobs' own
  tier assignment at its own call sites is untouched.** `decision.js`'s
  `decideAttention()` weighs a finding against real, live context (approved memories,
  via the same `approvedMemoriesText()` `prompt.js` already injects) rather than
  matching a fixed "emergency category" list, per the user's own explicit requirement
  that such a list breaks the moment their life or priorities change. One model call
  answers both the tier (1/2/3) and — only when quiet hours are active — whether this
  clears the emergency bar, to keep quota cost down. No model available, or an
  unparseable reply, is never treated as Tier 1 or an emergency by default; it falls
  back to Tier 3, a quiet record only — silence is the safe failure direction in both
  places. Verified live against a real model: a genuine financial-harm finding correctly
  came back Tier 1 (and, tested during quiet hours, a real stated emergency), citing an
  actual approved memory about the user's finances in its own reasoning; a mundane
  household reminder correctly came back Tier 3 in both cases.
- **Quiet hours gate LIVE SPEECH only, never the record.** `prefs.quietHours`
  (`{enabled, start, end}`, default on with a sensible night window — the one pref in
  this project that isn't an opt-in dial, since a fresh install should never get
  proactive contact overnight before the user has even seen the setting) is read by
  `quiet-hours.js`. The notification and outbox row a finding produces are created
  regardless of quiet hours — they only take effect once the user is already engaging,
  at which point quiet hours has nothing left to protect; only whether `speak.js`
  actually fires right now is gated. The bar for the emergency exception is explicit,
  reasoned text from the SAME decision call, not a second mechanism — "when genuinely in
  doubt, it is NOT an emergency" is stated directly in its own instructions.
- **Availability is two separate checks, not one, because an emergency should skip only
  one of them.** `presence.js`'s `isReachable()` (a tab connected AND the user recently
  active) is the hard requirement for any live delivery — no plausible way to reach them
  without it, emergency or not. `isBusy()` (a small, admittedly incomplete starting list
  of call-app process names via the same `control/ps-bridge.js` window/process listing
  `server/monitor/engine.js` already uses — a browser-tab call is not detectable this
  way at all) is the secondary dampener the build spec describes, and is deliberately
  skippable for a genuine emergency the same way quiet hours itself already is.
- **Real proactive speech is a genuinely new channel, not a repurposed one.** Nothing in
  the app could previously start a turn with no message from the user. `speak.js` pushes
  a real assistant message onto the active session (reusing `brain.js`'s
  `getActiveSessionId()` rather than reimplementing session resolution) and broadcasts a
  `proactive_message` SSE event; `public/app.js` plays it through a standalone
  `AudioPlayer`/`BrowserSpeaker` instance (never the shared voice-engine object, which
  has no turn of its own to attach this to) and briefly reflects it on the orb. Honors
  the existing "Speak replies" setting exactly as an ordinary reply would.

## Operational Awareness (`server/ops/*.js`, `server/cost/*.js`, `server/artifacts/*.js`)

Six capabilities, all sitting on top of the Heartbeat above rather than duplicating its
scheduling/triggering, that together let Jarvis know whether it's actually WORKING —
distinct from Self-Model (what Jarvis IS and can do) and Self-Improvement (what Jarvis
has LEARNED from its own past work). **All six items have real, verified work; two
narrower, disclosed gaps remain, each a deliberate scope decision, not an oversight** —
see `server/ops/CLAUDE.md`'s own status note before assuming full coverage. Item 4
(Verification)'s semantic half is wired at three of the four completion surfaces the
original spec named (artifact creation, Job completion, scheduled-task outcomes) — NOT
wired into consequential chat answers, the costliest and most frequent surface on a
routinely rate-limited roster, a real cost-scope decision this build does not make
unilaterally. Item 3 (artifacts) covers every format including `.pptx` now, but
`.pptx` carries a genuinely narrower verification confidence than `.docx`/`.xlsx` — see
`server/artifacts/CLAUDE.md`'s own writers section for why the one available
verification technique (round-tripping through this project's own readers) can validate
docx/xlsx fully but can't reach a PowerPoint deck's required slideMaster/theme chain;
open a generated `.pptx` in real PowerPoint before trusting it the way docx/xlsx are
trusted.

- **A generic, sequenced write-ahead activity trace, generalized off Jobs' own
  `job_trace` the exact same way `job_outbox` -> `outbox` was already generalized for
  the Heartbeat above.** `server/ops/ops-trace.js` (db.js migration 15) is a real
  rebuild, not an ALTER — SQLite can't relax a NOT NULL foreign key in place, and before
  this a trace row could only ever belong to a job. `jobs/job-store.js`'s own
  `appendTrace`/`getTrace`/`getTraceTail` are now thin wrappers over this file with
  `source:'job'` baked in, so every existing Jobs call site needed zero changes —
  verified against a real copy of the user's own database, all 156 pre-existing rows
  survived intact. A non-job writer's sequence numbering (scoped by `source`+`sourceRef`)
  is completely independent of any job's own numbering, confirmed live.
- **Cost tracking is automatic — no manual logging, ever** — a model turn's token usage
  is captured directly off data every adapter's own SDK response already carried and
  simply never read before (`message.usage` on Anthropic's `finalMessage()`,
  `usageMetadata` on every Gemini chunk, a `stream_options:{include_usage:true}` request
  flag away on the shared OpenAI-compatible adapter). TTS/STT usage is captured at their
  own call sites (character count, real connected seconds). **Three separately-labelled
  numbers, per the owner's own explicit requirement, never blended**: MEASURED (what was
  actually counted, always available), PROVIDER-REPORTED (a real balance straight from a
  provider's own account API — ElevenLabs, OpenRouter — polled by a plain periodic timer,
  deliberately NOT routed through the Heartbeat's own finding/decision machinery, since a
  silent maintenance refresh has no finding for `decision.js` to spend a model call
  judging), and CALCULATED (measured × a known price — `null`, never a guess, for
  anything with no price on record). **Deliberately does not hardcode dollar figures for
  this project's own cloud models** — the catalog names models ahead of any publicly
  verifiable pricing this build could honestly stand behind; the one built-in price
  asserted without an external source is that a local model costs $0. See
  `server/cost/CLAUDE.md` for the full module breakdown.
- **Cost-at-decision-time (item 6) turned out to be mostly already built.**
  `models/router.js`'s `scoreFor()` already weighted `entry.tier.cost` in every scoring
  branch (background work already the cost-heaviest, at `-2x`) — what was missing was
  that `tier.cost` was always a 1-5 name-regex guess, never a measured fact.
  `cost/advisor.js`'s `observedCostTier()` substitutes a real-price-derived value in the
  exact same 0-4 domain the guess already used (never a new scale), which is what makes
  it mathematically guaranteed to stay inside `scoreFor()`'s already-proven-safe bounds
  rather than something that needed re-tuning.
- **Environment awareness (item 5) is a live, rolling picture, not a single current-
  instant read** — the owner's own explicit requirement to track load over TIME, not just
  right now. `os.loadavg()` is deliberately never used (`[0,0,0]` unconditionally on
  Windows, this project's own target platform); real CPU% comes from diffing two
  `os.cpus()` snapshots' idle/total time across a 30s sampling interval. "Unusually high
  or climbing" is defined against THIS machine's own rolling median plus a hard
  sustained-duration requirement (5 continuous minutes) — verified against controlled
  synthetic data that a single spike produces no finding while a genuine sustained climb
  does, with the real numbers in the finding text. Reachability (models, connectors,
  voice services) is a single read across three subsystems that already track their own
  fact, never a new probe. Registered as a Heartbeat source with its own checkState
  dedup — the same "don't re-notify for the same still-true condition on every tick"
  discipline `jobs-source.js` already established. See `server/ops/CLAUDE.md`.
- **Self-diagnosis (item 1) is nine real checks plus real self-heal, not a plan.**
  `server/ops/diagnostics/` mirrors the Heartbeat's own `registerCheck({id, intervalMs,
  probe(), remedy?()})` plug-in shape. Malfunction checks include a genuinely
  synchronous (zero `await` between calls) Memory create-read-delete canary — structurally
  unobservable to anything else, not merely unlikely to be seen, since nothing else in
  Node's single-threaded event loop can interleave mid-call. Security checks are
  detection-only, per the owner's own explicit scope (no `remedy()` on any of them):
  a config/secret-file integrity check found and fixed a REAL bug in its own
  verification — a first, time-window-based "was this explained by our own recent
  write" heuristic let a later, unrelated external change slip through unflagged if it
  landed inside the same window; fixed by comparing the actual content HASH the app
  itself last wrote, not merely how recently. Self-heal reuses Jobs' own
  retry-then-escalate shape genuinely, not a re-implementation: one remedy attempt per
  NEW failure (never per tick), verified with both a synthetic self-healing check and a
  synthetic remedy-fails-anyway check through the real dispatch path.
- **Reasoning integrity (also item 1) is the owner's own "steer early, buffer only the
  riskiest" — both halves built and verified through a real `runTurn()` call against a
  stub model, not read through in isolation.** Steer-early extends the EXISTING
  `selfFocusSection()` push path (`self/self-model.js`'s `computeTurnSignals()`) with
  real evidence embedded directly — the actual matched-lesson text and real
  attempts/failures numbers for tools already used this turn — instead of only telling
  the model to go fetch it itself via a second `check_myself` call. Buffer-the-riskiest
  is deliberately the narrowest possible slice: ONLY a step where a real matched failure
  lesson fired (structurally only possible from a turn's SECOND step onward, after a
  tool call already happened — an accepted limitation `self-signals.js` already
  documents), text chunks held back instead of streamed live, released as one block
  once the step completes. The check itself: was `check_myself` actually called this
  turn? If yes, release silently. If no, still deliver the answer (never withheld) but
  log a real `source:'verification'` trace row. **A genuinely risky transcript-injection
  design (fabricating a tool call to force a real second model round) was considered and
  deliberately rejected** — no way to verify it wouldn't corrupt raw round-trip fidelity
  (Anthropic's `thought_signature`, Chat History replay) without live model testing this
  build didn't have access to; the shipped design never touches the transcript at all.
  Verified end to end: a stub model calling a tool with a real, matched failure history
  then answering WITHOUT consulting `check_myself` produced exactly one buffered chunk
  (proving the hold-back actually happened) and a real trace row; the same scenario WITH
  a `check_myself` call in between produced zero trace rows and identical delivery.
- **Verification (item 4) is real, mechanical AND semantic, and caught real bugs in its
  own testing at every stage before shipping.** `server/ops/verify.js`'s
  `verifyFileOpens()` re-reads a generated Office document through this project's OWN
  independently-built reader (`documents/index.js`) — the same discipline that caught a
  genuinely serious bug in the artifact writers themselves (see item 3 below). Wired at
  artifact creation: a failure deletes the bad file immediately rather than leaving it
  behind pretending to be real; a pass is meant to be RECORDED, and the first version
  silently wasn't — every kept artifact's `verified` column stayed permanently null even
  after a real check passed — found and fixed via this build's own direct-call testing.
  `verifySemanticMatch()` (one budgeted model call — "does this genuinely answer what
  was asked") is now wired at **Job completion** (`jobs/worker.js`'s
  `turn.reportedDone` branch, genuinely reusing the SAME `canAutoRetry`/escalate shape
  stall-detection already uses at the same call site, sharing the same `job.retries`
  counter — never a second recovery mechanism) and **scheduled-task outcomes**
  (`scheduler.js`'s `runTaskNow()`, for `prompt` actions — no existing retry loop there
  to reuse, so a mismatch just flips the run's own `ok` and folds the reason into
  `error`, its next scheduled occurrence already its natural retry cadence). Both
  verified via a real stub model through the REAL production dispatch path
  (`driveJob()`/`runTaskNow()` directly, real trace/outbox/run rows inspected after) —
  retry-then-corrects, retry-then-escalates, and matches-immediately all confirmed
  correct in both subsystems. **Deliberately NOT wired into consequential chat
  answers** — the costliest, most frequent of the four named surfaces on a routinely
  rate-limited roster; a real, disclosed, open item, not an oversight.
- **Output/Artifact generation (item 3) produces real, openable files — verified by
  actually catching a real cross-platform ZIP bug, not assumed to work because
  `Compress-Archive` is a proven pattern elsewhere in this project.** `.docx`/`.xlsx`
  writers (`server/artifacts/writers/`) are format-agnostic BY CONSTRUCTION — only these
  two need real assembly, everything else is written as given bytes. **A genuinely
  serious, live-caught bug**: both PowerShell's `Compress-Archive` (the exact pattern
  `skills/store/skill-zip.js` already used successfully for packing a Skill folder) AND
  even .NET's `[ZipFile]::CreateFromDirectory()` store every ZIP entry path with Windows
  BACKSLASHES when the entry name comes from directory traversal on this platform —
  silently invalid per the Open Packaging Conventions spec real Office requires (forward
  slashes), confirmed to make a freshly-written `.docx` fail to open at all, in this
  project's own reader AND (by the same spec violation) real Word. Fixed by building
  each ZIP entry with an EXPLICIT, hand-constructed forward-slash name via
  `ZipFile.Open()` + `CreateEntryFromFile()`, never derived from a filesystem path.
  Re-verified after the fix by round-tripping real generated `.docx`/`.xlsx` files
  through this project's own independently-built readers (`documents/docx.js`,
  `documents/xlsx.js`) — the honest verification technique available with no real
  Word/Excel to open a file in. **`.pptx` is now built too, on the same fixed
  foundation** (the same forward-slash-entry-name fix, confirmed carried over
  correctly), **but carries a genuinely narrower verification confidence, disclosed
  rather than glossed over** — a real deck needs a slideMaster/slideLayout/theme chain
  `documents/pptx.js`'s own reader never opens at all, so the round-trip technique that
  fully validated docx/xlsx can only confirm slide order and text content are right,
  not that the master/theme chain is real-PowerPoint-valid. See
  `server/artifacts/CLAUDE.md` for the full account; open a generated `.pptx` in real
  PowerPoint before trusting it the way docx/xlsx are trusted. **One honest capability
  ceiling, stated in the tool's own description**: raster/photographic image generation
  is not possible at all — no adapter does image generation, no image library exists in
  this project's five-dependency budget. `run_code.js` now also captures a sandboxed
  script's own generated output FILES (not just stdout/stderr) as real artifacts,
  through the exact same mechanical-verify-then-keep-or-delete path — one honest
  verification discipline, never a second one.
- **Four new tools, all `core:true, meta:true`, read-only except `create_artifact`, no
  confirm gate on any of them**: `check_spending` (item 2/6's conversational interface —
  the owner's own explicit choice: data/tool layer for this version, no dashboard screen
  until they've used it), `check_environment` (item 5's pull path — "can I actually do
  this right now," distinct from `check_myself`'s "is Jarvis itself malfunctioning"),
  `check_my_health` (item 1's pull path — "has anything gone wrong with you lately,"
  distinct from both), and `create_artifact` (item 3's write path — no confirm gate of
  its own; the "propose first" half of the hybrid creation model lives in a prompt
  instruction, a conversational judgment call, not a token gate).
- **Verified via multiple real fresh-install server boots, not just unit tests, at every
  stage of this build, including its own follow-up pass closing two disclosed gaps.** A
  completely empty `data/` directory booted against the real `server.js` migrated
  cleanly through every migration up to `user_version 19`, with every new tool present
  in `tools/index.js`'s own startup load log each time — proof none tripped the
  loader/runner circular-import invariant. A stub HTTP server reproducing an
  OpenAI-compatible streaming response's exact usage-chunk shape (empty `choices`, real
  `usage` object) confirmed the cost adapter reads it correctly end to end; a separate
  stub model, driven through a real `runTurn()` call, confirmed the reasoning-integrity
  buffering mechanism end to end; a real `curl` against a live running server's own
  `/api/artifacts/:id` route confirmed a generated file serves with the correct
  `Content-Type` and exact byte content; a stub model driven through the real
  `driveJob()`/`runTaskNow()` paths confirmed the verification-wired retry/escalate
  logic in both Jobs and the Scheduler, with real trace/outbox/run rows inspected after
  each scenario, not just the returned value.
- **A real, background-review-caught stored-XSS finding in the artifacts route, found
  and fixed the same session, after this subsystem's own initial "done" report.**
  `GET /api/artifacts/:id` rendered a generated file INLINE (this app's own origin)
  unless a `?download=1` query param was present — a plain link, an `<iframe>`, or a
  manually typed URL never carried it, and `create_artifact.js` places no restriction on
  what an `.svg`/`.html` file's own text content contains, so a crafted SVG's embedded
  `<script>` would execute in-app. Fixed: `Content-Disposition: attachment` is now
  unconditional, plus `X-Content-Type-Options: nosniff` and a sandboxing CSP header, plus
  CR/LF stripped from the filename before it lands in a header value (a second,
  independent finding folded into the same fix — an unsanitized filename could
  otherwise inject extra response headers). Verified against the real exploit shape: a
  real artifact holding a literal `<script>alert(1)</script>` payload, fetched via a
  real running server with the exact previously-vulnerable plain GET, confirmed forced-
  download headers; a separately crafted filename with a raw CRLF, fetched the same way,
  confirmed no header injection occurred.

## Self-Improvement (`server/improvement/*.js`)

Jarvis reviewing its own completed work, extracting observations, turning a genuinely
RECURRING one into a behaviour rule it applies to itself, and separately noticing
patterns in the user's own work/projects/study/finances/travel. A different mechanism
from Memory (`server/memory/`) — Memory learns durable facts ABOUT THE USER; this learns
about Jarvis's OWN performance and, on a much narrower separate track, the user's
non-personal activity patterns. See `server/improvement/CLAUDE.md` for the full
module-by-module breakdown; the decisions that matter beyond that file:

- **A layered chain, same shape as Jobs above, for the same reason.** `capture.js` (zero
  model calls, ever) writes an outcome the instant a job/task finishes or a plainly-
  worded correction fires — leaf-safe, callable from `models/runner.js`'s own turn loop
  and `scheduler.js`'s own outcome hook with zero latency risk. `reflect.js` (one batched
  call) turns a backlog of outcomes into individual lessons — observations, never a
  behaviour change by themselves. `synthesize.js` (one rarer call) looks ACROSS the whole
  active lesson set for something that genuinely recurred — a proposal is only ever
  created once at least two lessons, backed by at least two DISTINCT underlying outcomes,
  agree — and turns THAT into a proposal. `improvement-policy.js`'s `decide()` is the
  single seam every path (auto-apply, the screen's Approve button, a conversational
  `suggest_improvement` call) asks before anything real changes; `apply.js` is the only
  thing that ever writes a live rule or flips a pref, and the only thing that ever undoes
  one. This is what makes "detect a pattern, don't just patch a one-off mistake" real,
  not aspirational — a single job failure structurally cannot become a permanent rule.
- **The user's own settled requirement: only Jarvis's own directly-observed history may
  ever auto-apply — anything read from outside always asks, no matter how solid it
  looks.** `decide()`'s hard floors (none overridable by trust level): `kind` must be
  `'rule'` or `'setting'` (a Skill/code/idea/conflict always asks); `sourceTier` must be
  exactly 1; `conflictWith` must be unset. `synthesize.js` computes a rule proposal's
  `sourceTier` from the WORST tier among its supporting lessons — never hardcoded to 1 —
  specifically so a pattern that leans on even one outside-sourced lesson can't slip past
  this floor by riding along with genuinely tier-1 evidence. Two further modules,
  `improve-research.js` (tiers 2-4: official docs, communities, general web — reuses
  `server/research.js`'s own free-web-then-model-search path rather than reimplementing
  it, and requires a SECOND corroborating source before a lookup becomes a lesson at all)
  and `life-patterns.js` (patterns in the user's own non-personal activity), both feed
  this SAME pipeline — there is exactly one place a lesson becomes a proposal, never a
  second path for outside-sourced material.
- **Never proactively raises or infers about the user's emotional state or
  relationships — enforced in code, not just by prompt instruction.**
  `improvement/domains.js`'s `isExcludedDomain()` is applied on BOTH sides of
  `life-patterns.js`: matching memories/messages are filtered out of the model's input
  before the call, and any produced insight that still matches is dropped after.
  Deliberately biased BROAD, the opposite asymmetry from `personality.js`'s distress/
  serious-topic floors — there, a false positive only costs tone; here, a false negative
  means content the user explicitly excluded reaches a model call, which is the actual
  harm this module exists to prevent, so over-excluding costs nothing and is the safe
  direction. Verified against the same class of false-positive check `personality.js`'s
  own regexes needed: "I have a good relationship with this codebase" does not match.
- **A real spend cap, not a hoped-for cadence.** `improvement-store.js`'s budget ledger
  (`app_state`-backed, no new table) gives `reflect.js`+`synthesize.js` a combined daily
  budget and `improve-research.js`+`life-patterns.js` a combined weekly budget, on top of
  each module's OWN cadence floor (reflect: 4h, synthesize: 24h, both weekly modules:
  7 days). Without both, a 15-minute background tick (96/day) checking a simple "≥5
  unreviewed outcomes" count could fire on nearly every tick on a busy day. Confirmed
  live during this build's own verification: 10 compressed ticks in a row produced
  exactly 2 model calls, not 10.
- **Undo refuses instead of clobbering.** `apply.js`'s `undoChange()` compares the LIVE
  value against what the change actually set (`after`) before restoring `before` — if the
  user changed it themselves since (muted the rule on the screen, edited the pref), undo
  refuses with a plain "this changed since — restore anyway?" rather than silently
  overwriting their own later decision; `force:true` proceeds only after that's been
  shown to the user. An `undo` row is never itself undoable — bringing something back
  means approving a fresh proposal, not reversing a reversal, which is what keeps the
  append-only change log's `before`/`after` pair honest at every row. Verified live,
  browser-tested end to end: externally muting an applied rule, then clicking Undo on its
  Change row, produces the real confirm dialog, never a silent clobber.
- **Never volunteered in chat, only notification + log** — the user's own explicit
  choice. General-scope rules sit in `prompt.js`'s cacheable `stable` prefix (right after
  `memorySection()`, ungated by `background` — a rule learned from a job's own failures
  should apply to the NEXT job just as much as to a live conversation) with an explicit
  instruction never to bring one up unprompted; task/job-kind-scoped rules live in
  `volatile` via `opts.improvementScope` instead, since they vary per turn and would
  otherwise poison the Anthropic cache prefix for every turn that doesn't share the same
  scope. Auto-applied changes and pending-suggestion batches both surface via the
  notification bell (`addNotification`) plus a screen refresh (`broadcast`) — never a
  transcript system note. Verified live: the transcript stayed empty while the bell badge
  incremented and showed the real change.
- **Jarvis never edits its own code, structurally, not just by convention.** `apply.js`'s
  own header comment states the invariant it exists to protect: this module writes only
  SQLite rows and `prefs.js` keys, never a file under the repo source tree, and refuses
  outright (`applyProposal()`) for any proposal kind other than `rule`/`setting`. For
  anything needing real code (`kind:'skill'`/`'code'`), `implementation-prompt.js`
  generates a ready-to-paste brief for whichever coding assistant the user names —
  deliberately free text, never a hardcoded list — from a static, hand-written
  architecture summary that never reads the actual repo. Generating that brief IS the
  approval action for these two kinds on the screen, replacing the plain Approve button.
- **The `#/improvement` screen** (`public/screens/improvement.js`, nav group `About You`,
  beside Memory) is four tabs: Suggestions (pending — approve/reject, or for a `skill`/
  `code` idea, generate the coding-assistant brief, plus a "Show rejected" bin), Changes
  (the undo log, human-readable titles resolved from a change's own `after` snapshot
  rather than a raw rule id), Learned (live rules plus recently-noticed lessons, each
  with a "Show archived" toggle), Settings (the trust dial, the outside-research toggle,
  the two budget-remaining counters). Every row is clickable, opening a detail view —
  Rules are the one place text is directly editable, an edit riding the same undo
  machinery a freshly-applied rule already uses. Archive/Restore/Delete permanently
  (Rules/Lessons/Suggestions alike) deliberately reuse Memory's own proven archived-flag
  pattern rather than a new "Recycle Bin" concept — a real technical reason, not just
  consistency: a Rule's own Undo button in Changes depends on its row still existing, so
  a genuine hard-delete must always go through archive first, never be reachable
  directly from the live list. See `server/improvement/CLAUDE.md`'s own Phase 7 notes
  for two real bugs this first live user test surfaced and fixed: a tool declaration's
  own `description` is not enough to make the model actually call it without an explicit
  `SYSTEM_INSTRUCTION` trigger paragraph (unlike every other subsystem, this one
  shipped without one); and `reflect.js` was silently discarding real outcomes whenever
  its one model call failed, serious on a roster that's routinely all rate-limited at
  once.

## Self-Model (`server/self/*.js`)

Real, working self-knowledge — what Jarvis is, what it can and can't actually do, what
it's doing right now and why, how it knows what it claims to know, what's genuinely its
own call to make, how it specifically tends to fail, how it specifically works with the
user, and whether it's still on track toward what it's actually trying to accomplish.
Explicitly **not** an attempt at subjective experience or consciousness — that question
stays out of scope. See `server/self/CLAUDE.md` for the module-by-module breakdown; the
decisions that matter beyond that file:

- **The one rule everything else here answers to: every claim Jarvis makes about itself
  must be grounded in real evidence — a counter, a row, live state, or a real policy
  module — never a plausible-sounding estimate.** Below a minimum attempt count
  (`self-model.js`'s `MIN_ATTEMPTS_FOR_RATIO`, 5), a reliability check returns
  `no_track_record`, never a premature ratio. A self-model that narrates fluently about
  itself without this is worse than none — it's performed self-awareness rather than the
  real thing, which is the exact failure this build exists to avoid.
- **A layered read, same shape as Jobs' and Self-Improvement's own chains, for the same
  reason.** `self-signals.js` (zero imports, pure) decides only WHEN the self-model is
  worth consulting — five deterministic triggers, computed from plain data a caller
  already gathered. `self-capture.js` (zero model calls) turns a live tool outcome into a
  rolling reliability tally, and — only for a notable one — one more row in
  Self-Improvement's existing `improvement_outcomes` pipeline (never a second pipeline).
  `self-model.js` is the assembler: one builder function per dimension, dispatched by
  `buildSelfModel({only: [...]})` — omitting `only` builds nothing at all, never a
  default "everything." **The recorder itself has a health check, found necessary by a
  later audit, not designed in from the start.** `self-capture.js`'s `recordAttempt()`
  call used to run with no local error handling — a genuine SQLite write failure
  propagated straight out, caught only by `runner.js`'s own outer wrapper, logged and
  nothing else; a broken recorder and a tool genuinely never used were indistinguishable
  from every dimension reading `self_capability_stats`. Now wrapped locally, and either
  branch (success or failure) logs to `db.js` migration 11's `capture_health` table —
  `captureHealthSummary()` surfaces the last 24h's attempts/failures via `check_myself`'s
  `can_do` dimension, always included, so a `no_track_record` verdict can be told apart
  from "never used" versus "the recorder itself is broken."
- **Utterance provenance — the same later audit's harder finding, and the one still-open
  gap it explicitly could not close on its own: `check_myself` retrieves real data, then
  a model builds a sentence on top of it, and nothing checked whether the sentence used
  that data faithfully.** `server/self/self-verify.js`'s `verifyCitation(snapshotId,
  toolCallId, fieldName)` closes this for exactly one narrow, machine-checkable slice —
  NUMBERS, never free-form prose (deliberately out of scope; understanding whether a
  sentence's MEANING matches isn't solvable this way, and this file never tries).
  `db.js` migration 12 adds `self_model_snapshots` (the exact JSON every `check_myself`
  call actually returned, persisted permanently) and `self_model_citations` (every
  NUMERIC, checkable fact in a snapshot, logged as a citation candidate the instant the
  snapshot is taken — before anyone knows whether the reply that follows will use it).
  `verifyCitation()` never trusts a citation row's own stored value; it re-reads the
  snapshot AND the real persisted reply fresh every time (via `chat-store.js`'s
  `getMessages()`, correlating by the tool call's own id, never a new schema field on
  `messages` itself), and returns `used` / `ignored` / `unverifiable` — a free-text field
  is always `unverifiable`, on purpose, never guessed at. **A real bug this build's own
  verification caught before shipping:** the first version of the number-matching used
  plain substring containment, so a citable value of `0` falsely matched inside the text
  `"100%"` (`.includes('0')` is true for `"100%"`) — fixed with word-boundary-safe
  matching (`(?<!\d)0(?!\d)`), re-verified in both directions: a real citation is never
  missed, and a coincidental digit inside an unrelated longer number never falsely counts.
- **Goal-alignment grounding — the one audit finding that genuinely could not be closed
  with pure code, and this build stopped at that fork rather than faking a verdict.**
  Whether a `track_goal`-declared goal actually still serves what the user asked is a
  semantic judgment, not a fact lookup — no string match, however clever, proves or
  disproves it the way `self-verify.js`'s numeric citation check could. Three honest
  options existed (a small dedicated model call; a code-only keyword-overlap heuristic
  that could only ever say "some overlap" or "not enough signal," never really prove
  drift; or no pre-computed verdict at all). **The owner's own explicit choice: the
  third.** `track_goal.js` now snapshots the real text of the user's own most recent
  message the instant a goal is declared (`latestUserTurnText()`, reading
  `conversation.js`'s live window — the same one every adapter already builds a turn
  from, never a second source of truth); `db.js` migration 14 adds
  `self_goals.source_turn_text` to hold it. Dimension 9's own `instruction` then hands
  BOTH real texts to the model and tells it to judge freshly each time it checks in —
  aligned, drifted, or the original request was too ambiguous to judge — the same way
  dimension 6 already hands it real policy numbers instead of a pre-baked answer.
  Honestly `null`, with an instruction that says so plainly, whenever no source was ever
  captured — never a fabricated "aligned" default.
- **The last audit finding, the smallest of the four fixes: dimension 6's own explanatory
  prose could quietly drift from the live numbers sitting right beside it.**
  `whatsItsCall()`'s `hardFloor` (memory) and the first entry of `hardFloors`
  (self-improvement) used to be hand-typed sentences that never actually quoted
  `autoSaveThreshold`/`minEvidence` — accurate the day they were written, with nothing
  to catch it if a threshold ever changed later. `memoryApprovalFloorText()`/
  `improvementEvidenceFloorText()` (`self-model.js`) build the sentence FROM the live
  number as their own argument instead — the prose and the number are the same read now,
  not two that happen to agree. Regression-tested by mutating `memory-policy.js`'s
  `THRESHOLDS`/`improvement-policy.js`'s `MIN_EVIDENCE_BY_TRUST` directly at runtime
  (plain exported `const` objects, not frozen) and confirming the sentence picks up the
  new number — including the `Infinity` branch ("nothing auto-saves at all"), not just
  the finite-threshold wording. The three purely structural floors (kind, source tier,
  no conflict) were left untouched — none of them reference a live number, so there was
  nothing there for prose to drift from.
- **This reads Memory, Jobs, Self-Improvement, and Personality; it duplicates none of
  them, and the user's own settled decision on this exact question was to keep it that
  way.** Dimensions 3/7 ("how it behaves" / "how it fails") are a read-only VIEW over
  `improvement-store.js`'s existing `improvement_lessons`/`improvement_rules` — this
  directory owns none of that data. Dimension 4 ("doing now, and why") *reports*
  Personality's own already-computed `readStyle()` result; it never decides tone itself
  — Personality remains the sole owner of that decision, per the user's explicit
  constraint. The two tables this build DOES own (`self_capability_stats`,
  `self_goals` — `db.js` migration 9) deliberately hold no prose knowledge and no facts
  about the user, which is what keeps them from being the "second memory-like store" the
  build was told never to create.
- **The authority ceiling is enforced by the import graph, not a prompt instruction —
  the same technique `personality.js` uses to keep style from writing back to
  substance.** Nothing under `server/self/` imports `capabilities.js`, `tools/index.js`,
  `models/runner.js`, `scheduler/*`, or `control/session.js` — verified live, not just
  read: `node -e "import('./server/tools/index.js')"` loads cleanly with `check_myself`
  present, and a grep for those five paths across every file in the directory turns up
  only doc-comment mentions, never a real `import`. A strong self-assessment can inform
  how confident Jarvis SOUNDS about a claim's substance; it structurally cannot skip a
  confirmation, an approval, or any boundary `memory-policy.js`/`improvement-policy.js`/
  the confirm gate already enforces — there is no code path for it to travel through
  even if a future edit tried. `self-model.js`'s own `whatsItsCall()` (dimension 6)
  reads the REAL live values out of those policy modules (`memory-policy.js`'s
  `THRESHOLDS`, `improvement-policy.js`'s `MIN_EVIDENCE_BY_TRUST`) rather than
  paraphrasing them, so this can never quietly drift from what they actually enforce.
- **Hybrid trigger design, same general shape as Jobs' active-supervision pattern but a
  genuinely separate mechanism** — that one watches an external worker for a reason,
  this watches Jarvis's own state for a reason. Passive by default; `self-signals.js`'s
  five triggers (`authority`, `knownFailure`, `noTrackRecord`, `correction`,
  `blockedOnBackground`) are computed fresh every step of `models/runner.js`'s own
  tool-calling loop, cheap SQLite/state reads only — never a model call. **The push path
  has a real, accepted limitation, worth understanding before extending it:**
  `knownFailure`/`noTrackRecord` can only ever match a tool THIS turn has already called
  in an EARLIER step (`usedToolNames`, accumulated across the loop) — there is no way to
  warn about a tool before the model decides to call it for the first time, since
  nothing here has foreknowledge of that decision. The **pull** path
  (`check_myself`'s `can_do`/`failure_modes` dimensions) is what catches it proactively,
  before any use at all — `prompt.js`'s own system-instruction paragraph tells the model
  to check first rather than answer from impression whenever a claim about its own
  reliability, authority, or current state is about to be made.
- **`prompt.js` integration follows its existing stable/volatile split exactly.**
  `selfSection()` (stable, cacheable, no DB read — the one hard governing rule) and
  `selfFocusSection(signals)` (volatile, emitted only when a signal actually fired — a
  signal to weigh, never a scripted line to repeat back, same discipline as
  `floorsSection()`) are both deliberately **ungated by `background`**, same reasoning
  `improvementSection()` already uses: a background Job's own turn benefits from
  knowing it's blocked on something or heading into a known failure just as much as
  live chat does. `correction` simply never fires there, since `noteCorrection()` itself
  is gated on `!opts.background`.
- **Two tools, both `core:true, meta:true`.** `check_myself` is the pull path (no
  reliable search-intent text to find it by otherwise — "can you actually do this
  reliably" doesn't map to a capability search the way "what time is it" maps to
  "time"). `track_goal` is the write side of dimension 9 — records what Jarvis
  understands a live conversation's actual goal to be, always reported back as *what it
  recorded it understood*, never as a verified account of what the user meant; a job
  already has its own durable `goal` column and never needs this.
- **No screen.** Not part of the build's own request; the existing `#/improvement`
  screen already surfaces the data this directory reads (rules, lessons).

## Computer control (`server/control/*.js`)

Lets Jarvis actually operate the desktop — click, type, read windows, launch apps —
toward a stated goal, via its own loop, independent of `models/runner.js`'s chat loop.
See `server/control/CLAUDE.md` (loads automatically when working in that directory) for
the module-by-module breakdown. The items below are this build's own additions/decisions
on top of the loop that already existed — refined against the owner's original spec, not
a rebuild; see `server/control/CLAUDE.md`'s own intro for exactly what already matched.

- **A real control session has been watched completing a task end-to-end and
  independently verified against the real OS** (not just the loop's own claim) — this
  closed a long-open item, and required fixing a real, confirmed, pre-existing bug along
  the way (connector tool declarations leaking internal fields into Gemini's strict
  schema — see `server/control/CLAUDE.md`'s Gotchas).
- **Before it takes over, Jarvis now gives a real, in-character spoken heads-up** — a
  `prompt.js` rule specifically for `control_computer`'s SECOND, confirmed call (never the
  initial plan read-back): lead that turn with a brief, natural line letting the user know
  it's taking over now and to keep hands off, in its own words, never a fixed sentence.
- **Owner's decision: the browser Jarvis drives for real interaction stays its own
  separate, isolated instance — it does NOT reuse the user's actual open Chrome window.**
  Weighed directly against the user's own "reuse an already-open window" example and
  chosen deliberately for reliability (a real Chrome tab's accessibility tree isn't
  reliably readable the generic way; this connector's own CDP-based reading is what
  actually works) — see `server/connectors/CLAUDE.md`'s `browser.js` entry for the
  reasoning in full. The rule going forward: this visible window only opens when a task
  genuinely needs to click/type/interact with a page, or the user explicitly asked to
  browse — never for a plain lookup.
- **Most information lookups no longer risk popping a visible browser window.**
  `prompt.js` now steers a plain lookup toward `read_web_page`/`look_it_up`/the free
  research path (all invisible); the browser connector is reserved as above. It also
  gained a genuinely headless render mode (`renderPageHeadless()`) as `read_web_page`'s
  own fallback for a JS-heavy page a plain fetch can't read — so even that case stays
  invisible rather than falling back to a visible window.
- **Owner's decision: Screen Sharing is a real, persistent ON/OFF mode, not a one-shot
  glance.** (`server/control/screen-share-state.js`), distinct from `look_at_screen`'s
  one-off glance and a `screen_looks_like` monitor's condition-based watch — a manual
  header toggle and a spoken instruction ("share my screen with me"/"stop sharing") both
  read and write the same state, so either one is always reflected by the other. Turning
  it on never itself triggers a description — matches this project's existing "never
  speak unprompted" discipline (Heartbeat, Monitoring).
- **Owner's decision: real video via ffmpeg, not an animated GIF or the Windows Game Bar
  shortcut.** `take_screenshot` delivers an actual image into the chat (not just a spoken
  description, which is what `look_at_screen` already did); `start_screen_recording`/
  `stop_screen_recording` produce a real, playable `.mp4` via ffmpeg
  (`server/control/screen-recorder.js`, `gdigrab` desktop capture) and deliver it the same
  way. **The general, reusable rule this introduced**: any tool can now deliver a real
  image/video into the transcript by returning `ui_action:{type:'attachment', kind, url,
  mimeType}` on its result — see `server/tools/CLAUDE.md`'s own entry on this pattern and
  `public/CLAUDE.md`'s note on the front-end side of it.

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

## Adaptive Communication Register (`server/personality.js`)

Jarvis has one voice, not a set of switchable "modes" — no hardcoded `friendMode`/
`coachMode` personas anywhere. What varies is delivery (warmth, directness, playfulness,
formality, how hard it pushes back); what never varies is the actual conclusion. This
is deliberately two separable *inputs* to one model call, not two engines — there is no
code seam between "Jarvis reasoned" and "Jarvis spoke."

- **The one invariant this subsystem exists to protect: style is downstream of
  substance and cannot write back to it.** `personality.js` is a dependency-free leaf
  module (same circular-import discipline as `task-store.js`/`skill-files.js` — see
  "Gotchas" below) whose only output is prose appended to a delivery-instructions
  block. It has no import path to anything that shapes what Jarvis concludes, and
  never should — an edit here that wants to reach content belongs in `prompt.js`'s
  `SYSTEM_INSTRUCTION` (the judgment layer) instead.
- **Hybrid design: code computes safety floors, the model infers everything else.**
  `detectFloors()` is pure regex over the turn's own text — no extra model call, no
  added latency. It deliberately catches only two things in code: **distress**
  (narrow — self/situation-directed only, e.g. "I'm exhausted, nothing works"; heat
  aimed at a bug or tool does NOT fire it, since that reads as wanting speed, not
  care) and **serious topic** (real financial/health/legal/relationship stakes,
  keyword-based, backstopped by the model's own broader read). Both floors can only
  push the register toward *more* measured, never less — a false positive costs tone,
  never information. **Both regexes were broadened once already from real user
  testing**, not written once and trusted: `seriousTopic` originally matched only
  "medication," not "meds" (missed "skip my meds for a bit"); `distress`'s
  "feel like a failure" pattern originally required that literal phrase, missing
  "feel like SUCH a failure" — now tolerant of a few words of filler between "like"
  and the actual word, plus a standalone "I'm a failure" pattern with no "feel like"
  at all. Confirmed live afterward with a false-positive guard test ("I am the
  failure point in this design" must NOT fire) so the broadening didn't overreach.
- **Two hard rules live in the always-injected `STYLE_FRAMEWORK` text, written as
  unconditional** — never personal criticism, and genuine distress softens directness
  even over the user's own explicit request for bluntness. These sit above the user's
  own explicit style instructions, which in turn outrank whatever Jarvis would infer
  on its own. A third rule of the same shape, added after live testing surfaced a real
  gap: **if a moment genuinely calls for pointing someone toward crisis/emergency
  support, say "your local crisis line" or "emergency services," never a specific
  number like 988** — the model doesn't know the user's country, and naming one
  US-specific number as if universal is actively unhelpful outside the US. Confirmed
  live: a stacked distress+meds turn that previously named 988 now says "your local
  crisis line" instead.
- **Explicit requests don't just set the register — they also block an old,
  already-acknowledged concern from re-hijacking a moment the user asked to keep
  light.** Found live: after a serious exchange about financial risk, asking Jarvis to
  "keep this light, I just want to joke around" got a full paragraph re-litigating the
  earlier concern instead of staying light. `STYLE_FRAMEWORK` now says explicitly that
  a genuine concern can still be named *briefly* but must never take over a reply the
  user has asked to keep light. A related, still-open regex gap found the same way:
  `EXPLICIT_PLAYFUL_PATTERNS` didn't match "I just want to joke around" (only the
  more rigid "just joke around," no words in between) — same class of miss as the
  distress-regex ones above, not yet fixed.
- **Reactions scale with how funny something actually is, never a flat tic.** Found
  and fixed live in the same pass: "When something actually strikes you as funny, let
  that show for real, scaled to how funny it actually is" — mild amusement gets a
  mild reaction, something genuinely funny gets more, nothing funny gets nothing. No
  scripted "lol," no reflexive laugh line repeated regardless of content.
- **Explicit style requests are sticky per session** (`readStyle()`'s
  `sessionStickyStyle` Map, same lifetime/cleanup pattern as `runner.js`'s
  `sessionStickyModel`/`sessionUnlockedTools` — cleared together in
  `resetConversation()`). "Give it to me straight" holds across turns; a serious-topic
  or distress floor overrides for that one turn only, then the sticky style resumes —
  the user never has to re-ask.
- **The stable/volatile split (`prompt.js`) is what keeps this cacheable.**
  `STYLE_FRAMEWORK` (constant) goes in `stable`; `floorsSection()` (depends on this
  turn's text) goes in `volatile`, same discipline as `situationSection()`'s wall-clock
  time. Putting the framework in `volatile` would silently defeat Anthropic prompt
  caching for the whole prefix.
- **Scope: needs a real audience, not just `!background`.** `briefing.js`,
  `scheduler.js`'s own prompt-action turns, and a Job worker's turn (`jobs/worker.js`)
  all set `opts.background: true`, but only a briefing is actually spoken to the
  owner. `addressed: true` (set only by `briefing.js`) overrides the background gate
  for the style framework and floors specifically — `jobsSection()` stays gated on
  `background` alone, unaffected by `addressed`, since a briefing has no
  `check_on_work`/`stop_working_on` tools to act on it with. A scheduled task's own
  turn and a Job worker's own turn correctly get neither: nobody is being talked to.
- **Gemini Live gets the constant framework only, not per-turn floors — a real,
  accepted gap, not parity.** Live sets its system instruction once at
  `ai.live.connect()` with no per-turn refresh (`live.js`), so there is no hook to
  re-inject a per-turn computed floor mid-session. `live.js` was passing the bare
  `SYSTEM_INSTRUCTION` constant before this existed, which meant Live got neither hard
  rule at all; it now gets `systemInstructionParts({}).stable`.
- **No mechanism here builds domain expertise or frames analysis by topic** — that
  was an explicit scope decision, not an oversight. `personality.js` only ever touches
  *how* something is said; a domain-framing mechanism would be code shaping *what*
  gets concluded, which is the exact category this subsystem exists to keep out.
- **Debug-only visibility, off by default.** `runner.js`'s `runTurn()` yields
  `{type:'style_floors', floors, sticky}` only when a floor actually fired (same
  three-hop path as `model_switch`: `pipeline-engine.js`/`duplex-engine.js` re-emit
  it, `app.js` renders it as a system note) — gated behind `public/settings.js`'s
  `debugStyleFloors` toggle, default off. Nothing about this event is ever seen by
  the model itself.
- **On `autoSelect`, any prompt-driven behavior here is only as reliable as whichever
  candidate model actually answers this turn — and per "Free-tier quota varies wildly
  by model" (Gotchas, below), that is frequently a weak/free fallback model, not the
  user's preferred one.** Confirmed live during a
  real "why isn't this working" investigation: nearly every model in a real user's
  `data/models.json` was `state:'unreachable'` (quota/rate-limit/connection failures)
  at the moment of testing, leaving Auto to land on whichever free-tier model
  happened to be alive — exactly the kind of model least likely to reliably follow an
  unusual formatting instruction (see the reaction-marker section below). **The
  diagnostic technique that actually separated "is the code broken" from "is the
  model just not following instructions": write a small, read-only, one-off script
  that imports `models/registry.js`'s `getModel()` and `adapters/index.js`'s
  `getAdapter()` directly and calls `adapter.stream()` against one specific real,
  currently-working model entry, with the exact message text that was failing live.**
  This exercises the REAL production code (real system prompt via
  `systemInstructionFor()`, real adapter, real network call) with no stub and no
  server, and definitively answers "does the instruction work at all" independent of
  which model Auto happens to route to on a given turn. Touches no files, no server,
  no user data — safe to run anytime this exact class of "is it my prompt or is it
  the model" question comes up again.

## Real Vocal Laughter (reaction sounds)

Built on top of the Adaptive Communication Register above, but a genuinely separate
mechanism: a real, audible non-verbal sound spliced into playback, never text read
aloud as words. The trigger for this — the user explicitly rejecting a TTS voice
literally speaking "haha" — is why this exists as audio splicing rather than another
prompt paragraph.

- **Two real approaches exist; this project deliberately chose splicing a pre-recorded
  clip over asking a TTS voice to produce the sound live.** Investigated first: the
  free browser voice (`speechSynthesis`) has a hard ceiling — it hands plain text
  straight to `SpeechSynthesisUtterance` with zero channel for a non-verbal sound, no
  build can change that. ElevenLabs, as wired (`eleven_multilingual_v2`), has no
  non-verbal-sound feature either; their newer expressive model reportedly does, but
  that was never live-verified (blocked on account quota — 3 credits remaining, 46
  required for even one short attempt) and is not what routes the user's regular
  conversation. Splicing works identically regardless of which voice is narrating,
  since the clip isn't generated by that voice at all.
- **The model writes a literal token — `[[laugh]]` (`personality.js`'s
  `REACTION_MARKERS`) — at the point a real laugh belongs, per `STYLE_FRAMEWORK`'s own
  instruction.** That token must never reach the user as visible text or be spoken as
  words by any voice. Handled ONCE, server-side, in `runner.js`'s per-step loop
  (`createReactionScanner()`) — not duplicated across the transcript display and every
  playback engine — which strips the marker from the `chunk` text stream and yields a
  separate `{type:'reaction', kind:'laugh'}` event at the correct position. A consumer
  that doesn't know about reactions (today: nothing, but this is what makes it safe to
  add one later) just sees clean text with nothing missing.
- **The scanner is chunk-boundary-safe by necessity, verified against character-by-
  character streaming, not just whole-chunk delivery** — some adapters/providers
  stream far smaller pieces than a full marker. `createReactionScanner()`'s `feed()`
  holds back only as many trailing characters as could still be the start of a marker
  (including one that might turn out to have its own preceding space), never the whole
  buffer, so ordinary text is never meaningfully delayed.
- **Only the marker's own PRECEDING space is swallowed; the text AFTER it is left
  completely untouched, including its own leading space — found live, not assumed.**
  Swallowing both sides was tried first and produced words glued together with no
  space at all ("hilariousokay") once a consumer naively concatenates every `text`
  segment and ignores `reaction` entirely. Leaving the trailing space intact is what
  makes that naive concatenation still read as a normal, single-spaced sentence.
  `stripReactionMarkers()` (for the adapter's own already-assembled `finalEvent.text`/
  `callEvent.text`, which the scanner never sees) is built ON TOP of the same scanner
  (fed the whole string, then flushed) rather than a second implementation, so the two
  paths cannot drift apart.
- **`browser-speaker.js` was restructured, not just extended — a real, load-bearing
  architecture change worth knowing before touching that file again.** Before this, it
  fired each utterance straight onto Chrome's own internal `speechSynthesis` queue as
  soon as `pushText()` found a sentence boundary, relying entirely on Chrome to
  serialize playback. Chrome's queue only ever holds actual utterances — there is no
  way to interleave a raw audio clip into it. The class now owns an explicit
  `queue`/`_processing`/`_advance()` sequential model (utterances AND clips both),
  pulling one item at a time and waiting for it to fully settle before starting the
  next — audibly identical for plain speech (Chrome was already strictly serializing
  regardless of when `speak()` was technically called), but this is what makes
  inserting a clip at the right position possible at all. `audio-player.js` and
  `voice/playback.js` (the two OTHER independent sentence-chunked-queue
  implementations — see their own header comments for why three exist) needed only an
  additive `enqueueClip(url)` each, since they already had this exact sequencing.
- **Gemini Live is explicitly excluded — a real architectural gap, not deferred
  silently.** It streams continuous raw PCM audio directly from the model with no
  discrete "sentence" boundary at all, so there is no clean point to splice a separate
  clip into the way there is in every other, sentence-chunked path.
- **The current clip is a placeholder, not a real laugh** — `public/sounds/laugh.wav`,
  a synthesized two-note chirp built with zero external dependencies (a raw PCM WAV
  written by hand, no audio library), generated purely to prove the pipeline works
  end to end while ElevenLabs generation was blocked on quota. Swapping in a real
  clip once sourced is a one-file change (`public/reaction-sounds.js`'s
  `REACTION_SOUNDS` map) — nothing else in the pipeline needs to change.
- **Verification here required going further than reading code or curling an SSE
  stream — real audio playback needed a real browser.** A live `claude-in-chrome`
  session confirmed the marker never leaks into the transcript, `enqueueClip()` is
  called with the right URL, the server serves the asset correctly, and — this took
  isolating two different tools to actually prove — the WAV file itself is
  byte-perfect valid audio (`AudioContext.decodeAudioData()` succeeded cleanly) even
  though the `<audio>` element's own network loading stalled specifically inside that
  automated tab. That isolates a known category of CDP-automation quirk in
  `<audio>`/`<video>` element loading specifically (plain `fetch()` on the identical
  URL worked fine in the same tab) from an actual application bug — worth knowing
  before concluding audio playback is broken from automated-browser testing alone;
  confirm with the user's own regular browser instead.
- **The instruction is a real gap on weaker models, confirmed live, not yet fixed.**
  A real production call to the user's own currently-working free-tier model DID
  correctly write `[[laugh]]` when genuinely warranted, proving the mechanism itself
  works — but placed it at the very START of the reply and used it TWICE in one
  reply, both explicitly against `STYLE_FRAMEWORK`'s own instruction ("never at the
  very start... never more than once"). The scanner handles either case correctly
  (verified: marker-at-start, and multiple markers in one reply, are both in its own
  test suite) — this is a prompt-adherence gap on the model's side, not a scanner bug.

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
- **A tool contract communicated only as prose in the system prompt is not something a
  model can actually satisfy — it needs a real, declared, schema-visible argument.**
  Confirmed live: every confirm-gated tool's own `parameters` schema went unnoticed for a
  long time with no `confirm_token` property at all; `prompt.js`'s `SYSTEM_INSTRUCTION`
  told the model to resend one anyway, on trust. A model that sticks strictly to its own
  declared function-calling schema (common on weaker/free-tier models, per the Model
  system section's own note on `autoSelect` landing on whichever model is alive) had
  nowhere to actually put it, so a real user "yes" could go nowhere.
  `capabilities.js`'s `getToolDeclarations()` now injects it centrally
  (`withConfirmToken()`) rather than relying on 15+ individual tool files to each declare
  it themselves. The general lesson: if an instruction tells a model to send back a
  specific argument on a later call, that argument needs to actually exist in the tool's
  own declared schema — describing it in the surrounding prompt text is not enough.
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
- **On Windows, a ZIP entry's own path must be built as an explicit, hand-constructed
  forward-slash string — never derived from a filesystem directory traversal, even via
  a `.zip`-writing approach that's already proven elsewhere in this project.** Found
  building `server/artifacts/writers/` (real `.docx`/`.xlsx`/`.pptx` files): PowerShell's
  `Compress-Archive` — the exact pattern `skills/store/skill-zip.js` already uses
  successfully for packing a Skill folder — AND even .NET's own
  `[System.IO.Compression.ZipFile]::CreateFromDirectory()` both write every entry path
  with Windows BACKSLASHES when the entry name comes from walking a directory on this
  platform. That's silently invalid per the Open Packaging Conventions spec real Office
  formats require (forward slashes only) — confirmed to make a freshly-written `.docx`
  fail to open in this project's own `documents/` reader outright, and by the same spec
  violation, in real Word. The fix: open the zip with `[System.IO.Compression.ZipFile]::
  Open()` and add each entry via `ZipFileExtensions.CreateEntryFromFile(zip, sourcePath,
  entryName)` where `entryName` is a plain string this code builds itself (e.g.
  `'ppt/slides/slide1.xml'`), never something derived from `path.join()`/directory
  walking — see `server/artifacts/writers/office-zip.js`'s own header comment for the
  full account. **The general lesson: "this exact zip-writing approach already works
  elsewhere in the codebase" is not proof it produces spec-correct paths — verify by
  actually reading the entry names back out of a real generated archive, not just by
  confirming the file exists and has a `PK` signature.**
- **Any route that serves back content a model (or a user) could have put arbitrary text
  into must force `Content-Disposition: attachment`, unconditionally — never gated
  behind a query param or any other condition a caller might simply omit.** A background
  security review caught this as a real stored-XSS finding in
  `GET /api/artifacts/:id`: it only forced a download when `?download=1` was present, so
  a plain link, an `<iframe>`, or a manually typed URL rendered the file INLINE, in this
  app's own origin — and since `create_artifact.js` places no restriction on what an
  `.svg`/`.html` file's own text content contains (SVG genuinely executes an embedded
  `<script>` when rendered inline by a browser), a crafted file could run script in-app.
  Fixed by making the disposition header unconditional, adding
  `X-Content-Type-Options: nosniff` and a sandboxing `Content-Security-Policy` as
  defense in depth, and stripping CR/LF from the served filename before it lands in a
  header value (an unsanitized filename could otherwise inject extra response headers —
  a second, related finding folded into the same fix). **Apply this same discipline to
  any FUTURE route that serves a file whose content isn't 100% Jarvis's own fixed,
  hardcoded output** — a generated artifact, an uploaded file, anything a model wrote
  the text of.
- **`spawn('powershell.exe', ['-Command', ...args])` with `args` as SEVERAL separate
  argv entries is not "one command with flags" — it silently breaks on any value
  containing a space.** `powershell.exe`'s own CLI parser takes only the token
  immediately after `-Command` as the command name and re-interprets every token after
  that as "CommandParameters" it reconstructs itself, which does not reliably preserve a
  single argv item as one atomic string once it contains a space. Confirmed live, a real
  bug in `server/skills/store/skill-zip.js`'s `runPowerShell()`: this project's own
  install path (`...\CLAUDE PROJECT\Jarvis-001\...`) has exactly that space, and
  `Expand-Archive -LiteralPath <that path> ...` failed outright ("A positional parameter
  cannot be found that accepts argument"). **The fix, and the rule for any future
  PowerShell `spawn` call in this codebase**: build ONE fully-formed command STRING
  yourself (each value individually single-quoted, embedded `'` doubled per PowerShell's
  own escaping rule) and pass exactly one argv item after `-Command` — this is parsed as
  one ordinary command line with no reinterpretation to go wrong. **A second, distinct
  mistake in the first attempt at this exact fix, caught only by re-verifying
  immediately**: quoting the very FIRST token too (the cmdlet name itself, e.g.
  `'Expand-Archive'`) turns the whole line into a plain string expression rather than a
  command invocation — a different parse failure ("Unexpected token '-LiteralPath'...").
  The cmdlet name must stay bare; only the value arguments after it get quoted. See
  `server/skills/CLAUDE.md`'s Gotchas for the full account, including how it was
  re-verified (a scratch path deliberately given a space of its own, to reproduce the
  exact failure shape before trusting the fix).
