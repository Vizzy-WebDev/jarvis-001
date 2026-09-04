# Background Task Orchestration ("Jobs") — `server/jobs/*.js`

See the root `CLAUDE.md`'s "Background Task Orchestration (Jobs)" section for the
layered design and the decisions that matter beyond this file (the write-ahead trace,
stall detection, the third confirm mode, interruption tiers, the split depth ceiling).
This file is the module-by-module breakdown.

## The circular-import split — the one thing to understand before touching this directory

`orchestrator.js` imports `worker.js`, which imports `models/runner.js`, which imports
`capabilities.js`, which imports `tools/index.js`. That means **`orchestrator.js` is NOT
safe for anything under `server/tools/` to import** — doing so would recreate the exact
deadlock root `CLAUDE.md`'s circular-import invariant exists to prevent (`tools/index.js`
dynamically imports every file in `server/tools/` at load time, including whichever tool
made that import).

`job-actions.js` exists specifically to give tools a safe door in: it imports only
`job-store.js`, `job-events.js`, `job-policy.js`, and `../prefs.js` — all leaves — so it
is itself leaf-safe. **The rule going forward: any job action a tool needs to call
directly belongs in `job-actions.js`, never in `orchestrator.js`.** `orchestrator.js`
re-exports `createJobIfCapacity`/`cancelJob`/`resumeStuckJob`/`hasBackgroundCapacityNow`/
`runningSummaries` from `job-actions.js` unchanged, so `server.js`'s routes don't need to
know the split exists — they still just `import { ... } from './jobs/orchestrator.js'`.

A job action that actually needs to **start a worker** (not just change a row) cannot
live in `job-actions.js` — starting a worker means calling `worker.js`'s `driveJob()`,
which is exactly the non-leaf-safe edge. `job-actions.js`'s `resumeStuckJob()` resolves
this by only ever touching the database: it sets `status: 'queued'` with a `resumeNote`
column carrying the owner's guidance, and it's `orchestrator.js`'s own 5-second tick —
the ONE thing that's ever allowed to call `startWorker()`/`driveJob()` — that reads
`resumeNote` back out and clears it. A missed `jobEvents` emission only costs latency
until the next tick, never correctness, same as `server/monitor/engine.js`'s
`monitorEvents` two-channel split this whole pattern is copied from.

`orchestrator.js`'s own `resumeOrphan()`/`restartOrphan()` (a *real* crash, not a live
stall — see "Crash recovery" below) skip this indirection and call `startWorker()`
directly, since they're route-only (`server.js`), never called from a tool.

## `job-store.js` — the durable truth

Leaf module (imports only `db.js`). Three tables (`db.js` migration 4, plus migration 5
for `resume_note`):

- **`jobs`** — one row per job. `parent_id` is set ONLY by an approved split
  (`request_job_split.js`) and is ALWAYS the root ancestor's id, never an intermediate
  job's own id — see "Splitting" below for why that's what keeps the tree from ever
  exceeding depth 2. `conversation_id` deliberately carries no foreign key (a job must
  outlive the conversation that started it — same discipline migration 2 set for
  `memories` vs `memory_candidates`). `transcript` is a whole-snapshot overwrite (not an
  append) of `conversation.getMessages(sessionId)` after every completed step — what
  lets a `resumable` job continue with its real prior context instead of restarting from
  `goal` alone. `resume_note` is the tool-to-tick handoff described above.
- **The write-ahead activity log — table name is now `trace`, not `job_trace`.**
  Generalized off the original `job_trace` (db.js migration 15, root CLAUDE.md's
  "Operational Awareness" section) the exact same way `job_outbox` was already
  generalized below — `jobs/job-store.js`'s own `appendTrace`/`getTrace`/`getTraceTail`
  are thin wrappers over `server/ops/ops-trace.js` with `source:'job'` baked in, so
  every call site in this directory is unaffected and still just calls `appendTrace()`
  as always. `appendTrace()` takes `{phase: 'intent' | 'outcome', effect: 'read' |
  'workspace' | 'external', kind, summary, detail}` — a caller writes an `intent` row
  BEFORE an effectful action runs and an `outcome` row after, so a crash between the two
  still leaves the intent's `effect` on record. This single fact is what lets
  `job-policy.js`'s `classifyRecovery()` derive an honest resumability verdict instead of
  a job declaring one about itself.
- **The Tier 1/2/3 interruption queue — table name is now `outbox`, not `job_outbox`.**
  Generalized in db.js migration 13 (root CLAUDE.md's "Heartbeat" section) so a
  Heartbeat/Trigger finding with no job behind it could use the same broker — this
  directory's own `addOutboxEntry`/`listPendingOutbox`/`getOutboxForJob`/
  `markOutboxDelivered` (job-store.js) are thin wrappers over
  `heartbeat/outbox-store.js` with `source:'job'` baked in, so every call site here is
  unaffected. `prompt.js`'s `jobsSection()` drains it. `reason: 'permission'` (a parked
  confirm-gate decision, or a fresh `computer`-kind job asking to start) vs `'stuck'` (a
  stall/hang that survived its one retry, or the model's own `report_job_stuck`) — same
  delivery/resume path either way, just worded differently.

`RUNNING_STATUSES` (`queued`/`planning`/`running`) vs `RESOURCE_HOLDING_STATUSES` (those
plus `awaiting_decision`) are deliberately two different lists, found necessary by live
testing, not assumed up front — see root `CLAUDE.md`.

## `job-policy.js` — pure functions, zero imports

Same discipline as `memory/memory-policy.js`'s `decide()` — testable with a bare `node
-e` truth table, no server needed. `classifyRecovery(job, trace)` (crash verdict),
`diagnoseStall(tailTrace)` (five signals: exact repeat, oscillation, repeated failure,
near-duplicate reasoning, each returning `{cause, detail}` or `null`),
`stepBudgetExceeded(stepCount, kind)`, `isHung`, `hasCapacity`, `resourceAvailable`,
`canAutoRetry`. `STEP_BUDGET_BY_KIND` and `DIAGNOSE_TAIL_SIZE` (8) live here too.

## `job-events.js` — the in-process bus

`export const jobEvents = new EventEmitter()`. Same two-channel shape as
`monitor/engine.js`'s `monitorEvents`: `worker.js` and `job-actions.js` both emit
`'status'` events into it; `orchestrator.js`'s `startOrchestrator()` is the only
subscriber, re-broadcasting to the browser as `{type:'job_progress', jobId, status,
title}` via `events.js`.

## `worker.js` — drives one job

`driveJob(jobId, {allowedTools, kindByName, resumeText})`. A "step" means one
`runTurn()` call — it can still run several internal tool-call rounds
(`runner.js`'s own default `maxToolSteps`), but the outer loop only gets to inspect
what happened and decide continue/retry/stop BETWEEN whole `runTurn` calls, never
mid-call — two `runTurn` calls on the same session at once would race on
`conversation.js`'s session Map.

The model signals its own outcome via three internal-only tools
(`report_job_done`/`report_job_stuck`/`request_job_split` — see
`server/tools/CLAUDE.md`'s `internal` vs `meta` note), never a heuristic over prose.
After each `driveOneTurn()`, a `liveCheck` re-reads the job's own status: if anything
OUTSIDE this loop changed it (a `stop_working_on` cancellation, or `driveOneTurn`'s own
`onEscalate` callback parking it) the loop just returns, respecting whatever it is now
rather than overwriting it — the one point either kind of intervention CAN take effect,
since an in-flight `runTurn` call has no cancellation token (same accepted limitation as
`control/session.js`'s `raceAgainstStop`).

**`turn.reportedDone` doesn't mean "done" by itself any more — Verification (root
CLAUDE.md's Operational Awareness item 4) sits in front of it, reusing the SAME
`canAutoRetry`/escalate shape `diagnoseStall`'s own retry branch already uses below, at
the SAME call site, sharing the SAME `job.retries` counter — never a second recovery
mechanism.** `ops/verify.js`'s `verifySemanticMatch({request: job.goal, resultSummary:
turn.reportedDone})` spends one model call asking "does this genuinely answer the
goal." A `matches:false` verdict with `canAutoRetry(job)` still true pauses instead of
finishing — a trace row, `retries + 1`, a corrective nudge fed back as `nextText`, then
`continue`s the loop for one more attempt; a `matches:false` verdict with the retry
already spent escalates through the identical trace -> `awaiting_decision` ->
`addOutboxEntry(tier:1, reason:'stuck')` -> `notify()` path stall-detection already
uses. A job that already spent its one retry on a genuine stall gets no SECOND retry for
a verification mismatch, and vice versa — exactly because both branches gate on the
same counter. `checked:false` (no model available for the verification call itself)
never blocks a real completion — silence is the safe failure direction, same as
everywhere else this project applies it. **Verified via a real stub model through the
real `driveJob()` path**, not just read: a job whose reported summary never matches its
own goal retries once then escalates with real trace/outbox rows; a job that corrects
itself on the retry finishes normally with `retries:1`; a job that matches immediately
finishes with `retries:0` and no extra trace noise.

**`onEscalate` no-ops once the job is already `awaiting_decision`.** A confirm-gated
tool retried within the same `runTurn` call (before the outer loop gets a chance to
notice) produced up to 11 duplicate `job_outbox` rows for one decision before this
guard existed — found live, not by inspection; see root `CLAUDE.md`.

**`driveComputerJob(jobId, job, resumeText)`** is the one kind that bypasses the
tool-calling loop entirely, driving `control/session.js`'s `preparePlan`/
`runControlSession` directly instead. A `setInterval` heartbeat touch runs for the
duration of the call — `control/session.js`'s own loop can legitimately run for many
minutes with no hook to report progress mid-call, and without this the hang detector
would eventually "recover" a session that was never actually stuck, colliding with
`control/session.js`'s own `activeSession` singleton. Accepted tradeoff: this can't
distinguish "still working" from "hung inside one low-level step with no internal
timeout of its own" — `control/session.js`'s own `DECIDE_TIMEOUT_MS` already bounds the
common case.

## `orchestrator.js` — admission, the 5-second tick, crash recovery

`buildToolsetForKind(kind)` — `'generic'` gets every non-meta, non-internal capability
(`listCapabilities({includeMeta:false})`) with **no restriction at all**; `'research'`/
`'files'` filter that same list down to a small hardcoded `KIND_TOOL_NAMES` array PLUS
every currently-enabled folder Skill, unconditionally (`c.kind === 'skill'`) — a Skill is
the user's own packaged process, not a raw capability the kind is trying to fence off, so
every kind can reach one regardless of `KIND_TOOL_NAMES`. Before this, a `research`- or
`files`-kind job could not call an installed Skill under any circumstances, however well
it matched the job's actual goal — confirmed live, not assumed. `'computer'` never calls
this (see `driveComputerJob` above). Every kind gets
`report_job_done`/`report_job_stuck`/`request_job_split` appended regardless of what's
otherwise restricted — the completion/stuck/split vocabulary isn't something a narrower
kind should have to give up.

**What "kind" is NOT, worth being explicit about since the name invites a stronger
reading than the code delivers:** there is no per-kind system prompt, no role or
expertise framing, and the admission call's own `plan.summary`/`plan.steps` are
computed, shown in the UI, and never fed to the worker's own prompt at all — the
worker's first message is just the raw `goal` text plus the same generic completion
instructions every kind gets. `kind` only ever gates which tools are callable. Whether
to build a genuinely adaptable worker (the Orchestrator selecting tools AND framing per
task, not a fixed kind enum) is an open, undecided design question — see root
`CLAUDE.md`.

`tick()` (5s) starts anything `queued` (respecting `resourceAvailable()` — capacity
itself was already checked at creation, per `job-actions.js`'s `createJobIfCapacity()`)
and checks every `running` job this process is driving for `isHung()`.
`recoverFromHang()` spends the job's one retry, discards whatever the hung call may have
left in `conversation.js`'s live session, and resumes from the last durable
`transcript` snapshot instead of trusting it — the abandoned call, if it ever resolves,
finds the job already moved on.

`recoverOrphans()` — anything still `status:'running'` at startup crashed (no process is
running it, no heuristic needed). Classified via `job-policy.js`'s `classifyRecovery()`,
marked `orphaned` (never auto-resumed), reported via a Tier 1 outbox row. `resumeOrphan`/
`restartOrphan` are route-only (never called from a tool — see the circular-import note
above); `restartOrphan` refuses outright for `recovery:'unrecoverable'`.

## `tool-effects.js` — pure data, no imports

`classifyToolEffect(name, kindByName)` — a first-pass, hand-classified table (not the
final word; root `CLAUDE.md`'s Jobs section flags the real effect-based "must the owner
decide" classifier, reusing `control/guard.js`'s `classifyActionRisk` shape, as still
open). `kindByName` (built by `orchestrator.js` from `listCapabilities()`) is what lets
an unrecognized name default correctly: a connector tool (`kind:'connector'`) defaults
to `'external'` since connector names vary per user and can't be hardcoded; anything
else unrecognized defaults to `'workspace'`, the middle tier — matching
`control/guard.js`'s own "unknown ⇒ notable, never safe" spirit.

## `job-actions.js` — the leaf-safe action layer

See "The circular-import split" above for why this file exists at all.

- `createJobIfCapacity({title, goal, kind, resource, conversationId, plan, parentId})` —
  the ONE capacity-gated creation path, used by `/api/jobs` and `work_in_background.js`
  alike so they can never diverge on what "at capacity" means. `kind:'computer'` is
  special-cased: it's parked straight into `awaiting_decision` with a Tier 1 outbox row
  asking to start, never `queued` — an autonomous desktop-control session never begins
  unattended, full stop. `resource` defaults to `'computer'` for that kind so
  `job-policy.js`'s `resourceAvailable()` serializes it against any other computer-kind
  job, on top of `control/session.js`'s own independent `activeSession` singleton.
- `cancelJob(jobId)` — works regardless of current status, marks pending outbox entries
  delivered so a cancelled job's old decision never resurfaces on `jobsSection()` again.
- `resumeStuckJob(jobId, guidance)` — see "The circular-import split" above. `retries`
  resets to 0 (a human just intervened — a fresh automatic-recovery budget). Wording
  adapts on whether the job ever actually started (`job.startedAt`): "go ahead and
  start" for a fresh `computer`-kind confirmation vs "try something different" for an
  actual stall.

## Splitting (`server/tools/request_job_split.js`)

Deliberately self-contained — the whole judge-then-create flow lives in the tool's own
`run()`, not threaded back through `worker.js`'s core loop the way
`report_job_done`/`report_job_stuck`'s simpler true/false signals are. Cheap code-only
checks first (fewer than 2 or more than `MAX_PIECES` pieces, not enough capacity for all
of them), THEN one Orchestrator-level model call judging proportionality — deny on any
uncertainty, per the build spec's "uncertainty itself is a reason not to approve."

**The depth ceiling is structural, not a rule anyone has to remember to enforce:**
`const rootParentId = job.parentId || job.id` resolves to the root ancestor in exactly
one hop — a level-2 job's own `parentId` already IS the root. Every piece a split ever
creates therefore becomes a PEER under that same root, never a child of the job that
requested it. Confirmed live (not just by reading the code): a directly-planted level-2
job requesting its own split produced grandchildren whose `parent_id` was the ORIGINAL
root, not the level-2 job itself.

Approved pieces are always created `kind:'generic'` — deliberately, to keep a split's
total cost at one judgment call regardless of how many pieces it produces, at the cost
of a piece never getting its own specialized kind even when one would clearly fit
better (see "What kind is NOT" above — this is the sharpest version of that same gap).
