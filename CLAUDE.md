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
starts itself" is answerable by reading a single function.

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

## The provider and model system — `jarvis/models/`

The old one (`jarvis/model_system/` — a catalog, a router, fallback chains, per-task assignments,
the model detail screens) was deleted in full and its `ai_*` tables dropped by migration 26. **Do not
recreate any of it under another name.** What replaced it is deliberately small: a **provider
connection**, the **models listed under it**, and **one selected model**. That is the whole idea.

- **Connections and models** are two tables (migration 27: `model_providers`, `provider_models`).
  Keys live in `.env` via `save_secret`, named by `secret_ref` — never in a row, never in a response
  (`hasKey: bool` only). A model row is the provider's own id verbatim plus only what the provider
  reported that a request needs (`facts_json`: an output ceiling; the effort levels it accepts).
- **Six kinds, four wire formats** (`models/kinds.py`): OpenAI (`openai-responses`), Anthropic
  (`anthropic-messages`), Gemini (`gemini-generatecontent`), Ollama and LM Studio (both
  `openai-chat`), and Custom (the person picks the format). One module per format in
  `models/providers/`, each with the same three functions — `check`, `discover`, `stream` — and
  **nothing else in common**: no base class, no registry. OpenAI is NOT a gateway others go through.
  Raw `httpx` throughout; the provider SDKs in the venv are undeclared leftovers and are not used.
- **Testing, discovering and running are three separate questions.** Only a connection *test* sets
  its status. A failed *discovery* changes nothing and never blocks adding a model by hand.
- **Selected / available / executed are kept apart** (`models/selection.py`). The selection is the
  person's and only they change it. An unavailable one (connection deleted, key gone, model removed)
  is *reported*, by name, and stays selected. **A model the person NAMED is never replaced** — no
  fallback, ever; a failure is reported in the provider's own words plus a line saying Jarvis stayed
  on it and Auto exists. The only retry is the SAME model, twice, when the provider answered 5xx.
  `StepComplete.model_id` is what the provider *said* answered.
- **There is no "use"/enable step.** A discovered or added model is available; the composer picker
  (or Auto) is the only place a model is chosen. Do not re-add a per-model activation control.
- **Auto** (`models/auto.py`, `models/attempt.py`, pref `selectedAuto`) is the person's *other*
  choice, and the only thing that lets Jarvis pick. Deterministic, no chance. Candidates are models
  on usable connections minus those the PROVIDER reported as not chat/tool/image-capable (`facts`
  `chat`/`tools`/`image`/`free`, recorded by `openai_chat._facts` only from what a gateway reported —
  never guessed from names). Ranked: proven first (from `model_outcomes` or replies saved in the chat),
  then quicker speed class (<=3s / <=8s / slower; unknown = middle; a PREFERENCE only), then most
  recent success, then reported-tool-capable, then set-up order. A failed model waits behind the rest
  for 5 min x 2^(failures in a row - 1), capped at 2h, reset by any answer — one rule for every error
  kind. `auth`/`unreachable` failures hold back the whole connection; 402 (`billing`) holds back only
  models listed as paid.
  **Auto moves on ONLY when something actually failed — never because a model is slow.** There is no
  first-token timeout and no attempt-count cap. An accepted request is left to finish however long it
  thinks: the wire layer has only `SILENCE_CEILING_S` (600s of *total silence*, an inactivity limit
  that restarts on every byte) and TCP keep-alive (a live-but-busy server answers probes, a dead
  connection fails in ~60s). Attempts are bounded by evidence: 2 failed models on a connection and it
  is left alone for that step, one `auth`/`unreachable` failure leaves it at once, and after a failure
  `FIND_BUDGET_S` (30s) stops STARTING models that have never worked — it never touches a request in
  progress. No retry of a busy model while another candidate waits (going elsewhere beats asking
  again); the last candidate and a named model keep the 2 x 5xx retry. It never switches once a word
  has been spoken, announces every move as `ModelSwitched`, and names every failed model if all fail.
  `Resolved.proven`/`plan()` build one `Target` per connection (a per-candidate `.env` read cost
  seconds). Effort is not offered under Auto. Named models and pins bypass all of it.
- Facts about a model (`facts_json`) are still only what the provider reported: `maxOutput`, `effort`,
  and for gateways `chat`/`tools`/`image`. Existing rows get them on the next "Refresh models".
- **Effort is not a model and not a model property we know.** It is offered only for a model whose
  own provider reported levels (today, Anthropic's list API) and is sent only if that model was
  reported to accept it. `prefs.balance` (fast/balanced/quality) is a different thing — how much
  work *Jarvis* does around any model (tool-round ceiling, the answer check) — stored apart.
- **Wiring**: `assembly.get_orchestrator()` builds `models/client.py`'s `JarvisModelClient` (the
  orchestrator's port). `ai.ask()` runs on `models/oneshot.py`. **Only `client.py` imports the
  orchestrator** — `tests/test_architecture.py` enforces it, so a tool asking a question is never led
  into the turn loop. The cost ledger is fed by `runtime.publish_completed` (provider + usage, as
  reported).
- **Not implemented, and never claimed**: a provider's own speech-to-speech session (`/api/live` and
  `voice/options.realtime_models()` stay empty), web search through a model (refused, not answered
  from memory), and video/audio/PDF examination through a model.

The Model Settings screen (`components/screens/ModelsScreen.tsx`) and the composer's model picker
(`components/composer/ModelPicker.tsx`) share one hook (`lib/useModels.ts`); a change anywhere fires
`jarvis:models-changed` and every reader refetches. Speech-service keys (`external-services`) are a
separate system in the Settings panel and must not be disturbed.

Nothing under `jarvis/tools/` may import the orchestrator; `ai.py` is the seam a tool may use.

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
  that connection also takes a module `RLock` (`models/store.py`, `scheduler/task_store.py`).
  `tests/test_db.py::test_the_same_query_from_many_threads...` fails every run if the
  setting is removed. `store.write_json` retries `os.replace` briefly on Windows
  `PermissionError` (another thread reading the file at that instant).
- **A JSON tool schema is not "valid" just because JSON Schema says so.** Google refuses an
  `array` with no `items` — and refuses the whole request, so every turn that declares the
  tool, which for a core tool is every turn. Declare what an array holds
  (`test_every_tool_the_app_really_declares_is_acceptable_to_gemini` walks the real
  registry); `gemini_generate._schema` also narrows whatever a connector declares.
