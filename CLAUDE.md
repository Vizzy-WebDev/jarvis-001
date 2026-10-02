# CLAUDE.md

Jarvis: a local voice/text assistant. **Python + FastAPI backend, Next.js/React front
end built to a static export that FastAPI itself serves.** One process, one port, bound
to `127.0.0.1` only. Non-technical end user — keep error messages and setup steps in
plain language.

The behaviour this app promises is pinned by `backend/tests/contract/fixtures/` (42
recorded HTTP exchanges, replayed on every test run by `test_contract.py`).

## Run it

```
Start Jarvis.bat            # what the user double-clicks: first-run setup + launch + open browser
cd backend && python -m jarvis.main        # equivalent, for dev
```

Serves on `127.0.0.1:3000` (`PORT` overrides). `Start Jarvis.bat` finds Python, creates
`backend/.venv` and `pip install -e backend` on first run only, waits for the port to
actually answer, then opens the browser.

**Running Jarvis needs only Python.** Node builds the front end, and the build output
(`frontend/out`) is committed precisely so nobody running the app ever needs Node. When
the front end changes: `cd frontend && npm run build`, then commit `frontend/out`
alongside the source change. That is a developer step; a user never builds anything.

`.env` (git-ignored) holds API keys/secrets, written by `jarvis/config.py` — never
hand-edit the format, use `save_secret()`.

**Before killing/restarting the process, check whether the user already has an instance
running and is actively using it** (`ps aux | grep jarvis`, or `netstat -ano | findstr
:3000`). This project is tested interactively in the browser by the user, not just by
us — killing their server mid-session has happened before and is disruptive. Prefer
testing on a separate port or via static analysis over restarting their instance.

**The user may have a second Claude session working in this repo at the same time** —
files can appear mid-session, and a test port can already be occupied by the other
session's server. Put new work in new files, re-read any shared file immediately before
editing it, use small targeted `Edit`s rather than `Write` on anything shared, and pick
an unusual test port. **A restart that "succeeds" can still be talking to a stale
process** — `netstat -ano | findstr :PORT` (or `Get-NetTCPConnection -LocalPort PORT`)
is the only reliable check; a working `curl` afterward can still be answered by an old
process a failed kill attempt didn't actually reach.

## What runs on its own clock

Nothing here is a mystery ratio of "some things start automatically" — every
background-clock subsystem is gated behind its own `JARVIS_*` environment variable
(`ENABLE_ENV` on the module itself), defaulting OFF, so a test's own `create_app()`
never starts a real background thread unasked. `main()` — the real launch entrypoint,
never called by a test — sets every one of them via `os.environ.setdefault()` in
`_BACKGROUND_INTERLOCKS` **before** calling `create_app()`, so a real launch always has
all of them on: the scheduler (`scheduler/engine.py`), the heartbeat
(`heartbeat/engine.py`), the monitor (`monitor/engine.py`), the job supervisor
(`jobs/orchestrator.py`), cost/price refresh (`cost/balances.py`, `cost/prices.py`), the
environment sampler (`ops/environment/sampler.py`), Self-Improvement's own tick
(`improvement/cadence.py`), and three recycle-bin/logo sweeps added since — the
notifications trash purge (`notifications.py`), the connector icon resolver
(`connectors/icons.py`), and the chat-history trash purge (`chat_store.py`).
`assembly.start_background_work()` is the one place "what
starts itself" is answerable by reading a single function. One interlock gates work that
is triggered by a reply rather than a clock: `JARVIS_CONVERSATION_SUMMARY`, the running
conversation summary (`conversation_summary.py`, subscribed to `ASSISTANT_RESPONSE` in
`observers/recording.py`, run through `background.run_in_background`). Picking up
background work a restart interrupted is not a clock and has no interlock of its own: it runs
once inside the job supervisor's `start()` (specialist runs first, then jobs) and the
scheduler's `start()` (prompt runs), so it is on exactly when they are (`jarvis/durable.py`).

## Conversation context — `orchestrator/context.py` + `conversation_summary.py`

**There is no fixed context size anywhere, and none may be added.** Each turn the pipeline
asks the model client which model would answer and what it can hold
(`JarvisModelClient.context_capacity`, routed exactly like a real call), and
`context.budget_for()` turns that into room for the assembly — proportional reserves for the
reply, the tool declarations and a safety margin, converted from the model layer's
characters÷3 +10% estimate so Jarvis's own assembly can never trip `context_too_small`. A
model that never stated its window gets **no** limit from Jarvis; if it then refuses a
request as `context_too_long`, half the refused size is recorded as its learned window
(`models/state.record_learned_context`, folded into probes, only ever lowered) and the step is
re-assembled once and retried. When the budget is tight, earlier turns' tool results are
shortened before any whole turn is dropped; the current turn is never cut.

What scrolls out is kept by a **running summary** per conversation (migration 37,
`conversation_summaries`): structured notes (goal, facts, decisions, open items incl.
Jarvis's own promises, parked topics, current) written only from the transcript, anchored on
the saved message `seq` (`covered_seq`). A fold happens once verbatim history passes half the
model's budget (unknown window: only what leaves the in-memory working set), always in whole
turns, never the latest two. Edit/Retry into the covered range drops it
(`chat_store.truncate_to_before`). **Jarvis does not search past chats** — that feature
(`search_conversations`) was removed at the person's request; `track_goal` was replaced by the
summary's goal. The Chat History page's own search box is unrelated and stays.

## Testing

The project has a real automated suite. Use it.

```
cd backend && python -m pytest tests -q          # ~1300 tests
cd backend && python -m pytest tests/test_shell_e2e.py tests/test_content_e2e.py -q   # ~90 Playwright tests, real browser
cd frontend && npm run typecheck && npm run build
```

**On Windows, before you run them:**

- `test_shell_e2e.py` finds a Chromium already on the machine, per OS (`_find_chromium()`),
  and skips saying so if there is none. It used to look at one hard-coded Linux path, so the
  whole browser suite silently skipped on Windows. Its page-ready waits are tolerant
  (`visit()`/`refresh()`): a strict `networkidle` timed out at 30s in over a third of the
  tests while the same page loaded alone went quiet in under two seconds.
- `backend/.venv` may lack `pytest` and `playwright`. Do not `pip install` into it — the user's
  running app uses it. Put a `sitecustomize.py` that *appends* the global site-packages in a
  scratch directory and point `PYTHONPATH` at it; the venv's own packages still win.
- Seven tests assume "not Windows / no desktop" and fail there regardless of your change
  (`test_control_*`, `test_monitor`, and `test_tools::test_open_app_refuses_honestly_off_windows`).
  **That last one really launches Notepad** — `--deselect` it and close any stray one.
- **Any script that boots the app or reaches `get_db()` needs a scratch `JARVIS_DATA_DIR`
  exported first**, even a "quick import check" — it runs migrations on the real database.
- The provider layer is tested against `tests/stub_provider_server.py`: a real HTTP server
  speaking all four wire formats with real SSE streams. It cannot tell you what a *real*
  provider rejects — Gemini refusing an array with no `items` was found only by asking the
  real API, and fixed and guarded in `test_models.py`. Keep any live call to a few tiny
  requests (free tiers), with the real `.env` for READING and a scratch data dir.

Run the first two as two SEPARATE invocations, not combined into one `pytest tests -q`
call — putting ~1270 tests through one process has produced spurious browser-test
failures from resource pressure that disappear the moment the failing test is re-run on
its own. Two commands, both green, is the real signal; one combined run that shows a
handful of e2e failures is noise until each is confirmed to fail in isolation too.

Three layers, each catching what the others cannot:

- **Unit/integration tests** (`backend/tests/`) over the real modules.
- **The contract harness** (`test_contract.py`) replays 42 recorded HTTP
  exchanges — the durable record of the behaviour the API promises. A route that answers
  differently fails here even when its own tests pass.
- **Playwright** (`test_shell_e2e.py`) drives the built front end in a real browser
  against a real FastAPI on a scratch port — this is what catches a screen that renders
  but never calls its route, and any layout regression a component test cannot see.

**Testing must never touch the user's real `data/`/`.env`/port.** `jarvis/store.py` and
`jarvis/config.py` support `JARVIS_DATA_DIR`/`JARVIS_ENV_PATH`, and `PORT` overrides the
port — the `scratch` and `live_server` fixtures (`tests/conftest.py`) already wire all
three, so use them rather than rolling your own. A module that hardcodes a path relative
to its own source file bypasses this entirely — use `store.py`'s `data_dir()` for any new
`data/` subdirectory.

When a test genuinely needs a real credential, it is safe to point `JARVIS_ENV_PATH` at
the user's **real** `.env` (reading a secret touches nothing) while still using a scratch
`JARVIS_DATA_DIR` and a separate `PORT`. `tests/stub_oauth_server.py` is a real
PKCE-verifying OAuth server. Prefer a real stub server over mocking the module under
test — every subsystem verified that way found bugs that mocks would have hidden.

**To verify what a model actually DID, not what it said it did, read the real
`tool_calls`/`tool_results` payloads out of the `messages` table** (`jarvis/db.py`,
read-only, against `data/jarvis.db`). A model's own account of an action succeeding is
not evidence on its own; neither is a user's paraphrase of it. Confirmed live: a user's
summary of an exchange read exactly like a broken confirmation loop, and the stored
payload showed something completely different — and more serious.

Use the `Bash` tool's own `run_in_background: true` for anything that must outlive a
single tool call. A background process started with plain shell `&`/`disown` does not
reliably survive past that call; it can stop responding with no error at the point it
dies, and everything downstream then hangs looking exactly like a client-side bug.

**A command-prefix env assignment (`VAR=val cmd`) is NOT available as `$VAR` inside that
same command line's other arguments — confirmed live, the exact way it broke a real
pre-merge validation run.** `SCRATCH="<path>" python -c "os.environ['JARVIS_DATA_DIR']
= '$SCRATCH'"` looks right and isn't: `$SCRATCH` is expanded by the outer shell before
the prefix is ever applied, so it silently becomes `''`, `data_dir()` correctly falls
back to the real project `data/`, and the test writes into the user's actual data. Give
it a real `export` first, or write the literal path. Always `ls` the scratch directory a
script claims to have written to rather than trusting its own success output.

## Pre-merge validation gate

Before merging any branch into `main`, run the `pre-merge-gate` skill (`.claude/skills/pre-merge-gate/`).

## The rules that do not bend

**The import invariant.** Nothing under `jarvis/tools/` may import the loader, the
executor, the orchestrator or the gateway — directly or transitively. `load_tools()`
imports every module in that package, so an import back is a cycle that deadlocks
and looks like a hung server. A tool needing something only
the registry can answer receives it via `build(registry)`. `tests/test_architecture.py`
asserts this rather than trusting it.

**The turn loop watches nothing and imports no watcher.** `orchestrator/pipeline.py`
deliberately never imports cost tracking, the self-model, improvement capture, or
tracing — those subscribe to the event bus (`events/`) instead, via `observers/`, since
they RECORD what a turn did rather than shape it. `personality.py` is the one narrow,
deliberate exception: a dependency-free leaf imported directly to turn a literal
`[[laugh]]` token in the model's own streamed text into a real `Reaction` event —
transformation, not recording, so the restraint above doesn't apply to it.
`test_architecture.py` enforces the watcher restriction structurally.

**Risk is declared, never inferred.** Every `CapabilitySpec` must state `Risk.LOW/MEDIUM/HIGH`
— no default — so a capability cannot be treated as safe because someone forgot to
classify it. A `HIGH` risk capability with `retry.attempts > 0` is refused at
construction: that is how one "send message" stops being able to become three.

**A confirmation is a floor no instruction can lower.** The user's own explicit
in-the-moment "skip confirming" must never be honoured for anything effectful. A confirm
token minted in a turn cannot be redeemed in that same turn — enforced structurally, not
by prompt instruction, after a real model was caught completing an entire
ask-and-answer round trip with no human reply in between.

**Built-in tools are not Skills and must NEVER appear as one in the UI** — not in the
Skills screen, any browse/gallery view, or any picker listing "things Jarvis can do". A
Skill is knowledge Jarvis doesn't already have; never a rename of an existing ability.
Structurally enforced: a Skills UI may only read the folder-reading path, which has no
code route back to a built-in. This has regressed before despite being called out.

**Any route serving content a model or user could have written must force
`Content-Disposition: attachment`, unconditionally** — never behind a query parameter a
caller can omit. A stored-XSS finding in `GET /api/artifacts/:id` is why: an `.svg`
holding a `<script>` executed in-app when rendered inline. Also `X-Content-Type-Options:
nosniff`, a sandboxing CSP, and CR/LF stripped from the filename before it reaches a
header value. Showing such content IN the app is the front end's job and never an inline
route: it fetches the bytes and renders them as text, through `<img>`, or — a web page — in
a sandboxed frame without `allow-same-origin`, backed by `jarvis/request_guard.py`, which
refuses any `/api` request a browser labels `Origin: null` or `Sec-Fetch-Site: cross-site`.

**Approval floors never move for convenience.** A memory candidate that conflicts with
an existing memory always requires approval at every trust level. Only Jarvis's own
directly-observed history may ever auto-apply an improvement; anything read from outside
always asks. Jarvis never edits its own code.

**A style choice may only change HOW something gets said, never WHAT gets concluded.**
`personality.py`'s `STYLE_FRAMEWORK` and its per-turn floors (distress, serious-topic,
explicit direct/playful/devil's-advocate requests) shape delivery only; they're excluded
from a turn with nobody listening (`background=True` — a scheduled task's or a job
worker's own turn), matching `prompt.py`'s `has_audience` gate on `stable_instruction()`.

## The model layer — `jarvis/models/` (see its `README.md` and `DECISIONS.md`)

**Callers ask for what they need; the layer picks the model.** A `Request` states a task class, the
content (canonical items), the output it wants (text, or JSON with `required`/`best_effort`), hard
requirements, a **data class** (`public`/`personal`/`sensitive` — required, no default) and what to
optimize. Callers never name a model or provider; to steer they use **aliases** from config. The old
Kind table, Auto ranking, "proven" models, per-connection strikes, billing holds, `FIND_BUDGET_S`,
`same_model`, `CACHE_BREAK`, JSON-by-prompt and empty-reply-as-failure are gone — **do not recreate them.**

- **Public interface** (`jarvis/models/__init__.py`): `generate`, `stream` (canonical events; ends with
  `Done` or one `ErrorEvent`), `embed(space, inputs, data_class=)`, `explain_route` (ranked endpoints +
  every rejection reason, no call made — the same code path `generate` uses), `list_endpoints`,
  `refresh_catalog`.
- **Config is one YAML file**, `data/models.yaml` (in the data dir), merged over the shipped
  `jarvis/models/data/defaults.yaml`: connections (driver, base URL, `secret_ref`, trust class
  `local`/`zero_retention`/`standard`, limits, quirk profile, `default_params`, discovery, per-model
  overrides), aliases, routes per task class (+ `default`), policies, embedding spaces, quirk profiles,
  prompt profiles, settings. Validated on load; every problem reported at once, naming where it is.
  Keys are only ever in `.env` (`save_secret`); a key-looking field in config is refused. Saving from the
  settings screen rewrites the file (hand-typed comments aren't kept).
- **State is a separate file**, `data/models_state.json`: last discovery per connection (kept when a
  later one fails), probe results, latency, the per-endpoint circuit breaker, connection rests, month
  spend. **Traces** are the `model_traces` table (migration 36): one row per call, metadata only unless
  `settings.trace_content`; spend queries by connection / task class / day are GROUP BYs over it.
- **Endpoint** = one model on one connection, `connection/model-id`. **Capabilities** are a fixed,
  versioned vocabulary (`capabilities.py`), each value declared (driver default, quirk profile, config),
  discovered or probed — probed beats discovered beats declared. An unknown capability counts as absent.
- **Drivers** (`drivers/`): `openai_chat` (Chat Completions — OpenRouter, vLLM, LM Studio, Ollama,
  LiteLLM; server differences are quirk-profile **data**), `openai_responses` (stateless, `store:false`,
  encrypted reasoning), `anthropic_messages`, `gemini_generate`, and `fake` (scripted, for tests). Plain
  httpx via `drivers/_wire.py`; **status codes appear only in drivers**. A driver translates — never
  routes, retries or applies policy — and imports nothing of the app but `redact`
  (`tests/test_architecture.py` enforces it).
- **A request's path**: resolve (reject with a recorded reason: pin, health, key, **data policy**,
  capabilities, context estimate chars÷3+10% + per-image tokens, budget, max cost, schema) → route
  (`prefer` aliases, then the task class's route, then others only if `allow_others`; ordered by
  `optimize`; affinity key's last endpoint first) → adapt (prompt profile per family, foreign sealed
  items dropped **and reported**, canonical cache/effort hints — native encoding is the driver's) →
  execute → finish (structured output validated; emulated output repaired and buffered when streamed;
  usage + cost; provenance; feature report of native/emulated/dropped).
- **Execute**: retryable errors (`rate_limited`, `unavailable`, `timeout`) retry with backoff, then fall
  back **only within the filtered list**, preferring a different upstream; a server asking to wait >10s
  moves straight on. **Every other error ends the call** — including `auth` (billing/quota refusals map
  to it) and `context_too_long`. `allow_family_change: false` is honoured. **No fallback after the first
  streamed content event.** Per-connection concurrency/rpm limits, a 429 rests the connection, the
  breaker rests an endpoint after repeated failures (5 min doubling to 2h).
- **`data_class` is descriptive, not restrictive by default.** The shipped policy allows every data class
  on every trust class. A restriction exists only when `policies.data_classes` lists one — then it holds
  on every call: a pin never overrides it, fallback never relaxes it, and no allowed endpoint fails in
  plain language. Approved classes: the monitor's screen check is `sensitive`; CLI `--help` extraction
  and the public-YouTube fallback are `public`; everything else (every turn-loop role, `look_at_screen`,
  memory review, jobs, spreadsheets, skills, control) is `personal`.
- **Conversation state is the caller's.** Provider state (thinking blocks + signatures, encrypted
  reasoning, thought signatures, a server's own tool-call ids) travels as `Sealed` items tagged with the
  endpoint that made it and goes back only to that endpoint. **Tool-call ids are canonical** (made by the
  layer); the conversation store keeps each assistant message's output items in `raw` (`{"layer": 1,
  "items": [...]}`) so the next request carries them back unchanged.
- **Discovery** runs at startup (`JARVIS_MODEL_DISCOVERY`, in `main()`'s interlocks) and on
  `refresh_catalog()` — never on the request path. **Probing** is a CLI command
  (`python -m jarvis.models.probe`), cases as data, writes only to state, asks before a paid endpoint.
- **Embeddings** are named spaces (primary + declared identical backups + dimension); never a
  different model.
- **The boundary is two modules**: `client.py` (the turn loop's port — the only module that imports the
  orchestrator; maps each role to a task class and data class, the session to the affinity key, the
  selection to a pin, effort to a hint; maps fallbacks to `ModelSwitched` and usage to the cost-ledger
  event) and `oneshot.py` (behind `ai.ask`, whose callers must pass `data_class` and `task_class`).
  `settings.py` serves the Model Settings routes. **The person's chosen model is the alias `selected`
  (pinned); Auto is no `selected` alias**, so the task class's route applies. Effort is the
  `selectedEffort` preference, sent per request. A specialist's or scheduled task's model pin becomes an
  alias of the same name on first use (found or refused, never approximated).
- `prompt.py` produces an `Instructions` (`prompt_format.py`): labelled sections + the label of the last
  stable one — a `str` subclass, so text-only callers keep working.
- **Migrations**: 35 moves the old `model_providers`/`provider_models`/`model_outcomes` into config and
  state and drops them only after the export is read back and verified (a failure leaves them, tells the
  person, and startup retries); 36 is `model_traces`.
- **Not implemented, and never claimed**: web search through a model (refused plainly), provider-hosted
  tools, emulated tool calling, speech-to-speech, video/audio/PDF input, learned routing.

The Model Settings screen and the composer's model picker share `lib/useModels.ts`; the routes keep the
same JSON shapes (connection id = connection name, format = driver name). Speech-service keys
(`external-services`) are a separate system and must not be disturbed.

Nothing under `jarvis/tools/` may import the orchestrator; `ai.py` is the seam a tool may use.

## Durable background work — `jarvis/durable.py`

Background work — a job, a scheduled prompt run, a specialist run a restart cut off — runs in
**rounds**, and every finished round (its outcome and working transcript) is saved in
`data/durable.db`, so after a crash or restart the work continues at the round that was
running instead of starting over. **LangGraph (pinned in `pyproject.toml`) is used here for
persistence and orchestration of background work ONLY** — its functional API: one
`@entrypoint` per piece of work, one `@task` per round. Waiting for a person ENDS a run with
its place saved, and only an explicit answer starts the next — never LangGraph's
`interrupt()`, which a run picked back up after a crash answers with an EARLIER answer (found
by a real restart; `test_recovery_never_hands_an_earlier_answer_to_a_later_question`).
Never the live conversation, never a model client; `test_architecture.py` asserts LangGraph is
imported by `durable.py` alone and no LangChain anything exists. Each round is ONE ordinary turn
through the one turn loop — there is no second agent loop.

- **Nothing finished is done twice.** A round runs with `TurnRequest.operation_scope`, so a tool
  call's operation id is `<work>/e<epoch>/r<round>:<name>:<argument hash>#<n>` — the same when
  the round is replayed — and `capabilities/execute.py` returns the recorded result. Recorded
  LOW-risk failures of that round are cleared first (retried); an external failure is kept.
- **An action that may or may not have happened is never repeated unasked.** Every action a
  round starts is written down before it runs (the `started` table); an external one
  (MEDIUM/HIGH risk) with no recorded result stops automatic recovery (`durable.unsure`), and
  the person is asked to check it.
- `TurnRequest.continuable` + `Done.final_step`: a round that uses its last step writes a
  progress note; the next round carries on. `TurnRequest.max_steps` keeps a job's step budget
  (`STEP_BUDGET_BY_KIND`) exact.
- One call moves work forward whatever its state (`durable.advance`); a waiting piece of work
  continues only with an explicit answer, never from a plain start or recovery. One thread
  advances a piece of work at a time; `durable.stop` interrupts the round in flight.
- `Start Jarvis.bat` reinstalls when `backend\pyproject.toml` changed since the last install,
  so an update that adds a package never leaves an existing install unable to start.

## Specialist agents — `jarvis/agents/` (see its own `CLAUDE.md`)

Jarvis orchestrates; specialist agents (13 built-in, plus any the person creates on the
Specialists screen) are domain experts it hands work to with the one `ask_specialist`
capability, and they can ask each other. **Built-in and custom agents are the same kind of
row and run through the same path** — an ordinary turn given an `AgentBrief`, the agent's
access as `allowed_names` and its model as the existing pin. Do not build a second loop,
model system, tool system or memory for them. Tools/connectors are capabilities any agent may
be given; agents are split by responsibility, never by tool. Approval floors are unchanged
inside a specialist, and a slow specialist is detached, never cut off, with its result
delivered when it lands.

## Artifacts — `jarvis/artifacts/` (see its own `CLAUDE.md`)

Real files Jarvis makes: any format (Word, Excel, PowerPoint, PDF, and any text format —
Markdown, HTML, SVG, CSV, JSON, code). **Asked for → made at once, with no confirmation**
(`create_artifact` is `Risk.LOW` and `core`: the file stays in `data/artifacts/` and the person
can delete it); **not asked but clearly useful → offered in one sentence, made only after a
yes** (`prompt.py`'s `MAKING_ARTIFACTS`). Every artifact records the conversation that made it
(`conversation_id`, resolved from the session — a specialist's work belongs to the chat it was
asked in, a job's to none), so the chat shows it as a card that survives a reload and opens a
viewer, and the Artifacts page (`#/artifacts`) lists everything with Open in Chat (THAT chat,
landing on its card), Download, Copy and Delete. Only the person deletes.

## Content Management — `jarvis/content_manager/` (see its own `CLAUDE.md`)

Finished content taken through Review, Changes Requested, Ready to Post, Scheduling,
Published, Archived and a Recycle Bin. **It manages content and its workflow; it never
produces it, and it models no accounts, no creators' special paths and no publishing
provider.** Content enters through ONE door (`lifecycle.submit`) whoever brings it — the
person on the screen ("+ Add" inside a niche, one piece or several files at once — each file
its own item — with their own uploads), Jarvis (tools, including a
file attached in chat), or an agent (local HTTP API) — and every later action is the same
function for everyone. An item is the publishable thing; title, caption, hashtags,
thumbnail are its supporting fields and assets, from ONE registry (`kinds.py`,
`/api/content-meta`). A platform (placement) may use its own files/text per role, else the
item's. After approval the stage is DERIVED from the placements, and an item is LISTED under
every stage one of its platforms is in. A niche is a folder of many separate items (it can
exist empty, be renamed, and is deleted only when it holds nothing) — the niche held fixed as
a filter, never a copy and never one thing made of pieces. Publishing is a
hand-off queue — nothing here posts to a platform itself; which account a post goes out on
is the publishing tool's business. Reported numbers are dated snapshots (`cm_metrics`), never
calculated. Jarvis may hand in, edit text and schedule (the last two confirm first); only
the person approves, archives and deletes. The Recycle Bin is never emptied automatically.
Not to be confused with `jarvis/content/` (Content Analysis — unrelated, older).

## The Adaptive Communication Register — `jarvis/personality.py`

The tone/delivery layer. Regex-based
floors (`detect_floors()`) deliberately biased narrow — a false positive here only costs
tone, so "this fucking build is broken again" correctly does NOT read as distress aimed
at the user's own state. A sticky per-session style request ("give it to me straight")
persists across turns via `session_hooks.register_session_reset()`, the same registry
`session.reset_conversation()` calls into for every other per-session cleanup. Its
`ReactionScanner` turns a literal `[[laugh]]` token the model writes into a real,
separate `Reaction` event at the exact point `orchestrator/pipeline.py` turns streamed
text into a `Chunk` — the marker itself never reaches the transcript or gets spoken as
words by any voice; `frontend/public/sounds/laugh.wav` is the (placeholder) clip that
plays when it fires.

## Gotchas

- **Gemini's `thought_signature` must round-trip verbatim** on tool-calling turns — push
  the model's own response content back, not a hand-rebuilt object, or follow-up calls
  get rejected with a 400.
- **Streaming + function calls**: each chunk is an incremental delta, not cumulative.
  Concatenate parts across all chunks to reconstruct the turn.
- **On Windows, a ZIP entry's path must be an explicit, hand-built forward-slash string**
  — never derived from filesystem traversal. Backslash entry names are invalid per the Open
  Packaging Conventions spec real Office requires, and make a fresh `.docx` fail to open
  at all. Verify by reading entry names back out of a generated archive, not by
  confirming the file exists.
- **A tool contract communicated only as prose in the system prompt is not something a
  model can satisfy** — it needs a real, declared, schema-visible argument. A model that
  sticks strictly to its declared schema (common on weaker models) has nowhere to put a
  value the prompt asked it to send back, and the failure looks like an infinite loop.
- **The user's own running instance can restart itself mid-session**, independent of
  anything a Claude session did. The symptom is confusing: a route that obviously exists
  on disk 404s, because that process loaded an older snapshot. Check the real process's
  start time against the file's mtime before concluding your change is broken. **Never**
  restart it yourself to "fix" this.
- **A subsystem's own `CLAUDE.md` describing how something is wired can go stale the
  moment the wiring actually changes**, even when the code change itself is well tested
  — a doc saying "X calls Y directly" is a claim about the CURRENT call graph, not a
  historical note, and nothing enforces it staying true. Confirmed live, twice, during
  the same pass: `improvement/CLAUDE.md` and `self/CLAUDE.md` both kept describing a
  direct call from `orchestrator/pipeline.py`'s turn loop long after that call was
  replaced with an event-bus subscriber (`observers/improvement.py`,
  `observers/recording.py`) — specifically BECAUSE the turn loop must never import
  either subsystem directly. When changing how a capability's outcome gets recorded,
  grep the subsystem's own doc for the old call site before trusting it's still
  accurate.
- **The database is ONE connection shared by every server thread, and it must stay opened
  with `cached_statements=0`** (`db.py`). The driver caches prepared statements per
  connection by SQL text, so two threads running the same query at once share one
  statement object: errors ("another row available", "bad parameter or other API misuse")
  and — worse, because silent — rows handed to the WRONG caller. Measured: hundreds of
  errors and dozens of wrong rows per run with the cache on, none with it off. A store over
  that connection also takes a module `RLock` (`models/trace.py`, `scheduler/task_store.py`).
  `tests/test_db.py::test_the_same_query_from_many_threads...` fails every run if the
  setting is removed. `store.write_json` retries `os.replace` briefly on Windows
  `PermissionError` (another thread reading the file at that instant).
- **A JSON tool schema is not "valid" just because JSON Schema says so.** Google refuses an
  `array` with no `items` — and refuses the whole request, so every turn that declares the
  tool, which for a core tool is every turn. Declare what an array holds
  (`test_every_tool_the_app_really_declares_is_acceptable_to_gemini` walks the real
  registry); `gemini_generate.translate_schema` spells an open array `items: {}` (lossless).
