# Migration status — Next.js + FastAPI

Living progress record for the stack change. Update it as waves land; a
multi-session rewrite this size cannot be tracked from memory.

**Decisions (owner):** build Jarvis only in the new Python stack, `server/`
frozen · full parity, nothing dropped · redesign as I port · fix defects during
the port, disclosed · record-and-compare safety net first · desktop shortcut,
which settles the architecture as one process on one port · relevance-assembled
context (§23) · openWakeWord in the backend · **HIGH-risk actions require fresh
human confirmation even when triggered by scheduled tasks or briefings.**
psutil added for cross-platform system readings · semantic verification of
consequential chat answers built, behind a preference, default off.

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
| Built-in tools (§5) | **59 of 62** — every wave landed; the three not ported are named below |
| Scheduler + briefing (§13) | **Done** — tick loop off behind an interlock until cutover |
| Background jobs (§13/§32/§43) | **Done** — trace-based recovery, one retry, escalation |
| Sandbox + artifacts (§35/§45) | **Done** — isolation described as measured, files verified |
| Self-improvement · self-model · monitoring | **Done** — three floors, no premature ratios, honest refusals |
| Architecture fitness functions | **Done** |
| Turn API + event stream + approval routes | **Done** |
| Memory (§22) incl. importance/expiry | **Done** |
| Context assembler with relevance (§23) | **Done** — `RelevanceContext` |
| Wake word (§15) · conversation mode (§16) | **Done** (backend; a real utterance still needs a mic) |
| Cost tracking (§25) | **Done** — measured, provider-reported and calculated, never blended |
| Operational awareness: diagnosis, environment, verification | **Done** — one remedy per new failure; load judged against this machine |
| Heartbeat · triggers · proactive attention | **Done** — tick loop off behind an interlock until cutover |
| Content analysis (§20) · planning partner | **Done** — the free glance costs nothing; no question queue |
| Attachments · uploads · Office documents | **Done** — inline or registered, never auto-read |
| Folder Skills (§42) · pipelines | **Done** — two consent gates, stdlib TOML and zip |
| Connectors: files · MCP · API · CLI · browser | **Done** — OAuth deferred, see below |
| Secret handling: child environments · error redaction | **Done** — one scrub, one redaction, both asserted |
| Desktop (§): control loop · screen · recording · sharing | **Done in code; the last inch needs a Windows run** — see below |
| Front end — S1: the shell (design system, drawer, router, orb, transcript, composer) | **Done** — see below |
| Front end — S1.5: composer shape · Notifications · Scheduled Tasks · live approvals | **Done** — see below |
| Front end — S1.6: the task editor's connectors + model pickers | **Done** — see below |
| Front end — S2 models/keys · S3 voice · S4 the rest of About You + Automation · S5 Abilities | Not started |
| The 15 acceptance tests (§51) | Not started |
| Cutover | Not started |

`cd backend && python -m pytest tests -q` → **1073 passed, 23 skipped.** The
skips are contract fixtures for routes not ported yet, so the suite doubles as a
progress meter.

A fresh-install boot with the heartbeat and sampler interlocks ON (scratch data
dir, unusual port) migrates to `user_version 23`, registers and runs all nine
diagnostic checks, records their outcomes in the trace, takes a real system
sample, and answers a live route with 200 and an unknown id with a clean 404 —
no unhandled exception anywhere in the log.

## The front end, S1 — the shell

The interface has FOUR fixed anchors, agreed with the owner. Everything else —
the top bar, the drawer's insides, and the internal layout of all twelve section
screens — is a redesign, not a port of `public/`:

1. the hamburger is at the top LEFT and reaches every section;
2. the orb stays centred, with the mic beneath it, and nothing on the page moves
   or resizes it;
3. the conversation floats OVER the right edge of the stage, reserving no
   column and scrolling inside itself;
4. the conversation and the composer are ONE panel, not two.

**The anchors are tests, not a note.** `backend/tests/test_shell_e2e.py` drives
the REAL built export served by the REAL app on a scratch port, in Chromium:
it measures the orb's box before and after a long reply and an opening panel and
requires it unchanged, requires the stage to run underneath the floating panel
rather than beside it, requires the transcript and the composer to be inside one
element, and requires the page itself never to grow a scrollbar. A redesign that
quietly breaks one of those fails the suite — which a screenshot review does
not, and a component test cannot see at all.

What landed with it: the design tokens and the shared primitives every later
screen composes from (`components/ui/`), the SECTIONS registry as the one source
for drawer, hash router and (later) `open_section`, the orb in TypeScript, the
transcript streaming from `/api/chat/stream`, the composer with real uploads,
and the notification store + observer + routes behind the bell — a subsystem
that was publishing to an empty room until now.

The stub model on the far end of the wire is the project's own established
technique for a roster that is routinely all rate-limited; the adapter, the
orchestrator and the routes under test are the real ones.

## The front end, S1.5 — the composer, and the interactivity rule

Two corrections from the owner's first live look, and one standing rule.

**The composer was flattened in the port, and it mattered.** The original puts
the textarea on its OWN line and wraps the controls onto a permanent second line
below it — `public/style.css`'s `#composer-dock` comment records that as a real
bug fix, not a style choice: sharing the textarea's line is what made a grown
message clip the control row off. The port put attach · textarea · mic · send on
one inline row. Restored to three bands — attachments, text, then controls with
attach at the left and dictation + send at the right — which is also what makes
a 380px panel usable. **Attachments now scroll SIDEWAYS**: one row that never
wraps, so attaching a tenth file cannot grow the composer down into the
conversation. The panel itself narrowed 500px → 380px.

**The standing rule the owner set, which governs every screen from here:
if a thing is a thing, it is clickable.** No screen ships as a read-only display
of rows; every list of real objects gets a real detail view and real actions.
Landing with it:

- **Notifications** — every notice opens, reading it marks it read, and it can
  be deleted from its own detail. Backed by the store built in S1.
- **Scheduled Tasks**, brought forward from S4 at the owner's request. New
  routes `/api/tasks*` + `/api/task-runs` over the existing `task_store`, with
  the skill-name guard at the edge (the store is a leaf and must never import
  the capability registry). The on/off switch works from the list without
  opening anything — pausing is the thing people come here to do most. Two
  recorded contract fixtures went from skipped to passing.
- **The approval prompt in the transcript is now a real control.** It was a line
  of text you could not act on, which left the whole turn stuck: the run is
  genuinely stopped, waiting for that answer. The e2e test proves the tool did
  not run before Allow was clicked and did run after.

One declared divergence: `GET /api/tasks` returns a `descriptions` map (task id
→ the plain-English schedule sentence) alongside the recorded `tasks` list. It is
a sibling key rather than a field inside each task precisely so the recorded task
shape stays byte-identical and the addition is one top-level key `ADDED_KEYS` can
enforce. The sentence comes from `recurrence.describe()` — the same function the
spoken read-back uses — rather than a second implementation in TypeScript.

## The front end, S1.6 — the two options the task editor was missing

The owner spotted that the ported Scheduled Tasks editor had lost the original's
connector and model pickers. Restoring them turned up something worse first.

**A real bug the S1.5 tests could not see.** That editor wrote `action.prompt`;
`scheduler/engine.py`'s `_run_prompt` reads `action["text"]`. Every prompt task
created through the screen was accepted happily and then failed the moment it
ran, with "This task has nothing to ask." The e2e tests created, edited, paused
and deleted tasks — and never RAN one. There is now a test that builds a task
through the real screen and then actually runs it, which is the only shape of
test that could have caught this.

**Connectors** (`action.connectors`) hold connector IDS and are resolved to
capability names at RUN time, every run, by `connectors/capabilities.py`'s new
`tool_names_for()` — a connector's tool list changes when it is reconnected, and
anything that saved names once would go quietly stale. Picking connectors ADDS
them to what the task could already do: `engine._allowed_names()` returns every
non-connector capability plus the chosen connectors' tools, so the only things
excluded are connectors nobody picked.

**The model pin** needed real plumbing: `gateway/routing.py` has always accepted
a one-off `model_id`, but `TurnRequest` had no field to carry one, so a task
could not name a model at all. `TurnRequest.model_id` now threads through to the
gateway, and `Done` carries the model that ACTUALLY answered — not the same
thing, since a pin is an ordering the gateway can fall through, which is exactly
what makes a pin to a since-deleted model degrade instead of break.

`GET /api/models` and `GET /api/models/providers` came forward from S2 to feed
the picker (read only — every write stays in S2). Two more recorded fixtures pass.

**One harness fix this exposed.** `tools/record/proxy.mjs` scrubs any non-empty
STRING under a credential-looking key name before it reaches a fixture, so
`keyHint` is stored as the literal `<redacted>` and can never be compared. The
comparer now reproduces that exact rule on both sides
(`tests/contract/normalize.py`), counted like every other normalisation so the
"how much is being waved away" ceiling still applies. Presence, position and type
are still compared; only those strings' content is out of reach.

**Deliberately NOT built, at the owner's explicit narrowing:** any per-task
permission control. The rule set at the start of this migration stands unchanged
— a HIGH-risk action inside a scheduled task still asks a human, and nothing in
this screen can opt out of it. Also still absent: the original's `message` and
`briefing` instruction modes, both already handled by `_run_action` with no way
in from this screen, and its consent notice.

## The three Node tools with no Python equivalent

Named rather than left inside a count:

- **`report_job_done` / `report_job_stuck`** — a worker no longer announces its
  own completion with a tool call. `jobs/worker.py` reads the outcome from the
  turn itself and then verifies it semantically, which is a stronger check than
  trusting the worker's own claim. Deliberate, and already covered by the jobs
  tests.
- **`open_section`** — navigating the UI to a screen. There are no screens yet;
  it lands with the front end.

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
- **Every adapter discarded the usage the provider had already sent**, so spend
  could only ever be estimated. It is read where each wire format is understood,
  carried on the model port, and recorded off the event bus.
- **A free model read as "no price known"** — the $0 seeding sat behind the
  network interlock, and the provider price refresh was written, tested and
  never called by anything. Free and unpriced both come out as no money owed,
  and conflating them either understates spend or makes a genuinely free month
  look like missing data. Seeding now runs on every start (it needs no network
  and writes a fact); only the refresh is behind the interlock, and it finally
  has the timer it was missing.
- **A price row with both sides null costed usage at $0.00**, asserting "this was
  free" from no data. "A price row exists" and "a price is known" are now
  different tests, in both the report and the router's cost signal.
- **The config-integrity check compared a time window, not content.** Any
  external change to the secrets file landing inside the window was explained
  away by a write the app had nothing to do with. It compares the hash of what
  this app itself last wrote.
- **A camelCase split in a tool's DESCRIPTION re-created the false positive it
  was added to remove**: "Search SharePoint" became "search share point", making
  a harmless real tool risky. A name is an identifier where `updatePet` means
  update; a description is prose where SharePoint is a proper noun.
- **A capability calling another capability took a worker from the pool its own
  caller was waiting in** — with enough in flight that is a deadlock, and a
  deadlock there presents as the assistant simply stopping.
- **A Skill pipeline's confirm decision read the global registry** rather than
  the one being synced, so a sync against any other catalogue silently read the
  wrong risk levels.
- **A jobs test raced its own retry**: it forced a second stall while the retry's
  worker thread was still running, so the assertion depended on which thread
  wrote the status last. It only surfaced once the suite grew long enough to
  change the timing.
- **`prefs.maxBackgroundJobs` existed and nothing read it.** The orchestrator
  enforced its own constant instead, so lowering the limit changed nothing and
  the user still got three jobs at once. One reader now
  (`jobs/orchestrator.py`'s `active_job_limit()`), used by admission and by the
  split's own capacity check, with the constant as its default.
- **A CLI connector's program inherited every API key.** Saving a key writes it
  into this process's environment as well as the .env file, and the CLI
  connector launched its child with no environment of its own — so `git`, run
  from a saved template, had exactly the access to `GEMINI_API_KEY` that a
  sandboxed script is carefully denied. The sandbox's scrub is now a shared leaf
  (`jarvis/childenv.py`) used by both, and an architecture test requires every
  `subprocess` launch under `jarvis/` to name the environment it gives its child
  — with two exceptions, the desktop-shell and browser launchers, which hand a
  target to the user's own application.
- **Raw provider error text was stored and shown unredacted.** A provider that
  quotes the request back in its error — several do — put the key into
  `data/model-availability.json`, the log, and (for a connector) the tool result
  the model reads and the conversation saves. Node's `models/redact.js` was
  never ported; `jarvis/redact.py` is that port, applied at four chokepoints
  rather than by each caller: the availability record, the gateway's one source
  of failure text, the connector dispatcher, and an API connector's response
  body — where a query-auth service has just been sent the key in the URL.

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

- **The visible browser connector is wave H.** `read_web_page` renders a
  JavaScript-built page headlessly and invisibly, which is what a lookup needs;
  driving a real window someone can watch is desktop work and ships where it can
  be tested as it actually runs.
- **Connector OAuth is not built**, so the bundled connector directory is not
  ported either — every entry in it is an OAuth flow, and a directory of things
  that cannot be connected is the fake functionality §45 forbids. Custom MCP,
  API and CLI connectors that authenticate with a pasted key or token work in
  full. The contract fixture reports as deliberately-deferred, with the reason.
- **An MCP call connects per call rather than holding a process open.** A held
  subprocess is a lifecycle to get right — reaping it, noticing it died, not
  leaking one per connector — and a call is slower this way. The tool LIST is
  cached in the connector's own config and refreshed explicitly, so building a
  declaration never starts a server.
- **A Skill's helper scripts are Python only.** The sandbox isolates Python;
  shipping a runner for a language it cannot isolate would be a promise it
  cannot keep.
- **The desktop layer's last inch is not verified here, and cannot be.** The
  control loop, the guard, the approval flow, the capture stores, the screen
  tools, the routes and the monitor conditions are all exercised for real
  against a `FakeDesktop` that records every call in order — which is what
  catches sequence mistakes like typing before focusing. What that cannot cover
  is pywinauto and pyautogui actually moving a real mouse, reading a real UI
  tree, and posting a real WM_CLOSE. `python -m jarvis.control.selfcheck` is
  that check, and it runs on the owner's machine: it opens a scratch Notepad,
  does one primitive at a time, verifies each against the real OS, and prints
  pass or fail per line, including a real ffmpeg recording decoded back to prove
  the file plays. Until that has been run and read, this half is "written and
  reviewed", not "known to work".
- **The visible browser window is unverified for the same reason.** Navigation,
  reading, clicking and typing are tested against a real page over real HTTP,
  headless, using the same code the visible window runs — but that a window
  opens, is visible, and carries its own profile needs a desktop.
- **A CLI connector cannot be given ONE secret.** Its program now runs with the
  same scrubbed environment a sandboxed script gets, which is the fix; what does
  not exist yet is the deliberate opposite — handing one connector one
  credential on purpose. Nothing known needs it (`git`, `gh` and `aws` read
  their own config files rather than the environment), and the shape when it is
  wanted is a per-connector `envRefs` — `{"GH_TOKEN": "<secretRef>"}` resolved
  through `get_secret` at call time. An explicit grant, never inheritance.

- **Chat answers are verified AFTER they are given, not before**, and behind a
  preference that is off by default (`verifyChatAnswers`). Gating a reply would
  put a model call in front of every substantial answer and would mean the turn
  loop importing the verification subsystem, which the fitness tests forbid. So
  a mismatch is recorded and raised as a notice rather than withheld. Stated as
  the trade-off it is; the owner asked for the mechanism to exist and to be
  theirs to switch on.
- **Balance polling covers OpenRouter only.** It is the one provider whose
  credentials this build currently stores; ElevenLabs and Deepgram arrive with
  the external-service key store in wave G. Adding one is a single
  `register_reader()` call.
- **Commitments are watched via the structured `expires_at` field**, not by
  parsing dates out of prose. A date extracted from free text by regex is wrong
  often enough that the notifications would be wrong often enough to ignore, and
  this channel only works while it is trusted.
- **`is_busy()` cannot see a call in a browser tab.** It reads process names, so
  it is a dampener rather than a gate — and it is skippable for an emergency,
  which is why it is a separate function from `is_reachable()`.

- **Monitoring covers only the cross-platform conditions.** Watching a window, a
  process or the screen needs the desktop bridge (wave H); those kinds are
  refused by name, with what it CAN watch instead, rather than accepted into a
  watch that would never fire.
- **`analyze_spreadsheet` is not ported yet.** The readers it needs exist; its
  INPUT does not — it reads a file the user attached, and attachments arrive
  with wave G. Porting it now would mean a tool with nothing to read.
- **The desktop tools (7) are deliberately last**, so they can be tested on the
  real Windows machine rather than shipped having only had their off-Windows
  refusal exercised.
- **A real "hey Jarvis" has never been said to this build.** The model is proven
  not to wake on silence or noise, and every piece of plumbing around detection
  is tested with the score forced — but recognising a genuine utterance needs a
  microphone and a person. Owed by the front-end work.
- `prompt.py` carries only what is true of THIS build. Sections for projects,
  connectors, computer control, improvement and self arrive with their
  subsystems; describing them earlier would be fake functionality aimed at the
  model.
- `session_hooks.py` — later modules must register their per-session cleanup or
  "new chat" silently leaks sticky model, unlocked tools and sticky style.
- `run_code`'s sandbox description must describe the boundary as it actually is
  before that tool is ported (§35/§45).
- `GET /api/prefs` now returns one key Node does not have (`verifyChatAnswers`).
  The recording is untouched; the divergence is declared by name in
  `tests/test_contract.py`'s `ADDED_KEYS`, and the recorded keys must still match
  byte for byte — so it can only ever admit an added key, never a changed one.

## Things to carry into later waves

- `control/recording_store` and `screenshot_store` must derive their paths from
  `store.data_dir()`. The Node versions do not — a live test-isolation hole.
- Background engines own timers. Keep every Python scheduler OFF behind a flag
  until cutover, or they act on shared state twice while Node is still running.
- Verify Gemini Live against the INSTALLED Python SDK's source, not its docs.
- Chat streaming moves off SSE-over-GET only AFTER cutover, so the fixture
  harness stays valid across the whole port.
