# CLAUDE.md

Jarvis: a local voice/text assistant. **Python + FastAPI backend, Next.js/React front
end built to a static export that FastAPI itself serves.** One process, one port, bound
to `127.0.0.1` only. Non-technical end user — keep error messages and setup steps in
plain language.

This replaced a Node.js + Express implementation, retired at the S6 cutover. That build
is gone from the tree but remains in git history, and the record of what it actually did
survives in `backend/tests/contract/fixtures/` (45 recorded HTTP exchanges, replayed on
every test run by `test_contract.py`). Session-by-session build history — how the
project got here, decisions made along the way, real bugs and how they were found —
lives in `handoff-archive.md`.

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
environment sampler (`ops/environment/sampler.py`), and Self-Improvement's own tick
(`improvement/cadence.py`). `assembly.start_background_work()` is the one place "what
starts itself" is answerable by reading a single function.

## Testing

Unlike the Node build, this one has a real automated suite. Use it.

```
cd backend && python -m pytest tests -q          # ~1300 tests
cd backend && python -m pytest tests/test_shell_e2e.py -q   # 65 Playwright tests, real browser
cd frontend && npm run typecheck && npm run build
```

Run the first two as two SEPARATE invocations, not combined into one `pytest tests -q`
call — putting ~1390 tests through one process has produced spurious browser-test
failures from resource pressure that disappear the moment the failing test is re-run on
its own. Two commands, both green, is the real signal; one combined run that shows a
handful of e2e failures is noise until each is confirmed to fail in isolation too.

Three layers, each catching what the others cannot:

- **Unit/integration tests** (`backend/tests/`) over the real modules.
- **The contract harness** (`test_contract.py`) replays 45 real HTTP exchanges recorded
  from the Node server. It is the durable record of the behaviour this app promised
  before the rewrite, and it outlived the implementation it was recorded from. A route
  that answers differently fails here even when its own tests pass.
- **Playwright** (`test_shell_e2e.py`) drives the built front end in a real browser
  against a real FastAPI on a scratch port — this is what catches a screen that renders
  but never calls its route, and any layout regression a component test cannot see.

**Testing must never touch the user's real `data/`/`.env`/port.** `jarvis/store.py` and
`jarvis/config.py` support `JARVIS_DATA_DIR`/`JARVIS_ENV_PATH`, and `PORT` overrides the
port — the `scratch` and `live_server` fixtures (`tests/conftest.py`) already wire all
three, so use them rather than rolling your own. A module that hardcodes a path relative
to its own source file bypasses this entirely — use `store.py`'s `data_dir()` for any new
`data/` subdirectory.

When a test genuinely needs a real, already-configured model, it is safe to point
`JARVIS_ENV_PATH` at the user's **real** `.env` (reading a secret touches nothing) while
still using a scratch `JARVIS_DATA_DIR` and a separate `PORT`. All of the user's models
being rate-limited at once is the normal state, not an edge case: `tests/stub_openai_server.py`
is a real HTTP stub speaking the genuine OpenAI wire format, and `tests/stub_oauth_server.py`
is a real PKCE-verifying OAuth server. Prefer a real stub server over mocking the module
under test — every subsystem verified that way found bugs that mocks would have hidden.

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

Run this against a scratch instance — never the user's real port or data — before
merging any branch into `main`, and report each result plainly rather than summarising
as "passed":

1. **Syntax sweep** — `python -m compileall backend/jarvis` and `cd frontend && npm run
   typecheck`. Zero failures.
2. **Fresh-install boot** — start the real `jarvis.main` in the background
   (`run_in_background: true`) with `JARVIS_DATA_DIR`/`JARVIS_ENV_PATH` pointed at empty
   scratch paths and an unusual `PORT`. Must come up with no unhandled exception.
3. **Migrations + tool loader** — read the scratch `jarvis.db` read-only and confirm
   `PRAGMA user_version` reached 23: `jarvis/migrations.py`'s `MIGRATION_SQL` (19,
   the ones ported byte-for-byte from the Node build) plus
   `migrations_extra.py`'s `EXTRA_MIGRATION_SQL` (4, this build's own). Counting only
   the first file gives 19 and a false failure — the gate caught exactly that mistake
   in this document. **The database is created lazily on first use**, so hit a route
   before looking for the file. Confirm `load_tools()` (or a route that touches the
   capability registry) succeeds with no import error — currently 60 tools across 33
   modules; a real launch's own startup log is not a reliable place to see the count,
   since nothing in this project configures root logging by default and `jarvis.*`
   loggers have no handler attached unless something else in the process added one.
4. **Route smoke test** — `curl` a real GET (200), a route taking an id with a
   nonexistent one (a clean 404, not a crash), and `/` (the real `index.html` from the
   static mount).
5. **Any recently-fixed security behaviour** — re-confirm it live rather than by reading
   the code, e.g. `GET /api/artifacts/:id`'s forced-download headers on a plain request.
6. **The full suites** — `pytest tests -q` and the Playwright file, run as the two
   separate commands above, both green.

Teardown: stop the scratch server by the PID actually bound to the scratch port, delete
the scratch directory, and confirm the user's real instance/port is unaffected.

## Structure

```
backend/
  jarvis/
    main.py           FastAPI app + router mounting + the static mount for frontend/out. 127.0.0.1 only.
    assembly.py        The composition root — builds the registry/orchestrator once; start_background_work().
    routes/           One module per area, mounted in main.py. A surface over subsystems, no business logic.
    config.py         .env read/write (get_secret/save_secret/delete_secret). JARVIS_ENV_PATH override.
    store.py          Atomic JSON read/write for data/*.json. JARVIS_DATA_DIR override. data_dir().
    db.py             The one SQLite connection; migrations.py + migrations_extra.py hold the schema (23 total).
    session.py        Owns the active conversation id. session_hooks.py: per-session state cleared on "new chat".
    conversation.py   Neutral, model-agnostic transcript store.
    chat_store.py     Conversation/message CRUD + full-text search over SQLite.
    prompt.py         The shared system instruction, with its stable/volatile split.
    personality.py    The Adaptive Communication Register — tone floors, sticky style, real vocal laughter.
    capabilities/     The contract (spec.py), the registry, and execute.py — the one dispatcher.
    orchestrator/     pipeline.py: the turn loop. context.py, model_port.py.
    catalog/          What a model IS: family, version, capabilities, effort scheme. Owns nothing.
    gateway/          Deployments, connections, slots, routing, effort, availability, latency, probing.
    adapters/         One module per wire format: anthropic, gemini, openai_compatible.
    policy/           approvals.py, decide.py — the permission layer, independent of model behaviour.
    events/           The typed event bus. observers/ subscribe to it (cost, security, verification, improvement...).
    intent/           The fast-path router: what needs a model call and what does not.
    tools/            Auto-loaded built-in capabilities. See its own CLAUDE.md.
    skills/           Folder Skills ONLY — SKILL.md instructions. Never a built-in tool.
    connectors/       MCP/API/CLI/browser/files connectors, the catalogue, OAuth, icons.
    memory/ jobs/ scheduler/ improvement/ self/ heartbeat/ ops/ cost/ artifacts/
    control/ monitor/ sandbox/ content/ projects/ documents/ tts/ stt/ voice/
                      Each has its own CLAUDE.md — read it when working in that directory.
  tests/              ~1300 tests, plus contract/fixtures/ (45 recorded Node exchanges).
frontend/
  app/page.tsx        The shell: stage, orb, conversation panel, composer, drawer, router.
  components/         screens/ (one per section), ui/ (the shared primitives), conversation/, stage/.
  lib/                api.ts (the ONE typed client), api-types.ts, nav.ts (the SECTIONS registry),
                      voice/ (three engines, players, turn detector), useHashRoute.ts.
  out/                The BUILT export FastAPI serves. Committed on purpose — see "Run it".
data/                 (git-ignored) JSON stores + jarvis.db (SQLite).
```

## The rules that do not bend

**The import invariant.** Nothing under `jarvis/tools/` may import the loader, the
executor, the orchestrator or the gateway — directly or transitively. `load_tools()`
imports every module in that package, so an import back is a cycle (in the Node original
the equivalent deadlocked and looked like a hung server). A tool needing something only
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
code route back to a built-in. This regressed three times in the Node build despite
being called out each time.

**Any route serving content a model or user could have written must force
`Content-Disposition: attachment`, unconditionally** — never behind a query parameter a
caller can omit. A stored-XSS finding in `GET /api/artifacts/:id` is why: an `.svg`
holding a `<script>` executed in-app when rendered inline. Also `X-Content-Type-Options:
nosniff`, a sandboxing CSP, and CR/LF stripped from the filename before it reaches a
header value.

**Approval floors never move for convenience.** A memory candidate that conflicts with
an existing memory always requires approval at every trust level. Only Jarvis's own
directly-observed history may ever auto-apply an improvement; anything read from outside
always asks. Jarvis never edits its own code.

**A style choice may only change HOW something gets said, never WHAT gets concluded.**
`personality.py`'s `STYLE_FRAMEWORK` and its per-turn floors (distress, serious-topic,
explicit direct/playful/devil's-advocate requests) shape delivery only; they're excluded
from a turn with nobody listening (`background=True` — a scheduled task's or a job
worker's own turn), matching `prompt.py`'s `has_audience` gate on `stable_instruction()`.

## The Adaptive Communication Register — `jarvis/personality.py`

The tone/delivery layer, ported after the S6 cutover (S7) — a real gap the migration
initially left behind, not present under any name until this file existed. Regex-based
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

- **Gemini model names deprecate fast and docs pages are unreliable.** When a model or
  API shape matters, check the installed `google-genai` package directly or hit the live
  API — not a fetched doc page, which described an API that did not exist in the SDK.
- **Free-tier quota varies wildly by model and drifts over time.** Some models are
  retired entirely for new keys. Verify against the live API rather than a remembered
  number. On `autoSelect`, prompt-driven behaviour is only as reliable as whichever
  candidate actually answers — frequently a weak free-tier fallback, not the preferred
  model. The technique that separates "is the code broken" from "is the model just not
  following instructions": a small read-only script that calls one specific, known-working
  model entry through the real adapter with the exact failing text.
- **Gemini's `thought_signature` must round-trip verbatim** on tool-calling turns — push
  the model's own response content back, not a hand-rebuilt object, or follow-up calls
  get rejected with a 400.
- **Streaming + function calls**: each chunk is an incremental delta, not cumulative.
  Concatenate parts across all chunks to reconstruct the turn.
- **SDK error messages are raw JSON**, not human-readable, and each provider buries the
  real text at a different depth. Every adapter has a friendly-error path — follow it for
  any new error surface shown to the user.
- **On Windows, a ZIP entry's path must be an explicit, hand-built forward-slash string**
  — never derived from filesystem traversal. Both `Compress-Archive` and .NET's
  `CreateFromDirectory()` write backslash entry names, which is invalid per the Open
  Packaging Conventions spec real Office requires, and makes a fresh `.docx` fail to open
  at all. Verify by reading entry names back out of a generated archive, not by
  confirming the file exists.
- **`spawn('powershell.exe', ['-Command', ...several args])` breaks on any value
  containing a space** — this project's own install path has one. Build ONE fully-formed
  command string (each value single-quoted, embedded quotes doubled) and pass exactly one
  argument after `-Command`. The cmdlet name itself must stay unquoted; quoting it turns
  the line into a string expression rather than an invocation.
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
