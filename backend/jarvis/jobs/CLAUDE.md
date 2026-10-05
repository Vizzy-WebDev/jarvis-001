# Background Task Orchestration ("Jobs") — `jarvis/jobs/`

See the root `CLAUDE.md`'s "Background Task Orchestration (Jobs)" section for the layered
design and the decisions that matter beyond this file. This file is the module-by-module
breakdown. A job is work somebody, or Jarvis, decided to put in the background right now. It
needs supervision, recovery and an escalation path that a scheduled task does not.

## Import shape

`orchestrator.py` imports `worker.py`, and `worker.py` reaches the turn loop and the capability
registry only through **lazy imports inside `run_job()`** (`assembly.get_orchestrator()`,
`assembly.get_registry()`). That keeps `worker.py` and `orchestrator.py` safe to import at
module load time, which matters because `tools/job_tools.py` and `tools/job_split.py` import
`jobs.orchestrator`, and everything under `tools/` is loaded by the registry's loader (see the
import invariant in the root `CLAUDE.md`). Keep new top-level imports in these two files
leaf-safe, and put anything that reaches the orchestrator or registry inside the function.

## `job_store.py` — the durable truth (leaf; imports only `db.py`)

- **`jobs`** — one row per job. `parent_id` is set ONLY by an approved split and is ALWAYS the
  root ancestor's id, never an intermediate job's, which is what keeps the tree from ever
  exceeding depth 2. `conversation_id` deliberately carries no foreign key: a job must outlive
  the conversation that started it. `ACTIVE_STATUSES` is `queued`, `running`,
  `awaiting_decision`. `heartbeat()` stamps liveness (unrelated to `jarvis/heartbeat/`).
- **The write-ahead trace** — `append_trace()` / `get_trace()` / `get_trace_tail()` are thin
  wrappers over `ops/trace.py` with `source='job'` bound. Every tool call a job's round makes
  gets an `intent` row (from `TOOL_STARTED`, carrying `name`, redacted `args` and
  `operationId`) BEFORE it runs and an `outcome` row (`TOOL_COMPLETED`/`TOOL_FAILED`, same
  `operationId`) after — written by `worker._observe`, the durable runner's subscriber for the
  job's session. `effect` is `external` for a MEDIUM/HIGH-risk capability, `read` for LOW
  (`durable.is_external`). That is what lets `policy.classify_recovery()` derive an honest
  verdict, and what `diagnose_stall()`'s repeat/oscillation checks read. Other rows: `decision`
  (parked on an approval), `note` (finished, stalled), `nudge` (a retry or the person's
  guidance).
- **The Tier 1/2/3 interruption queue** — `add_outbox()`, `list_pending_outbox()`,
  `mark_delivered()` and `deliver_all_for_job()` are thin wrappers over `heartbeat/outbox.py`
  with `source='job'` bound. `orchestrator/context.py` drains pending rows into the next turn
  the user starts. `reason` is `permission` (a parked approval, or a desktop-control job asking
  to start), `stuck` (a stall or hang that survived its one retry) or `crashed`.

## `policy.py` — pure functions, zero imports

Run on every supervisor tick for every active job, so: no model calls, no side effects, no I/O,
and checkable as a truth table (the same discipline as `memory/policy.py`).
- `classify_recovery(job, trace, recorded)` — a job found `running` at startup with nothing
  running it. The work is durable, so finished rounds and recorded actions are never repeated;
  what is judged is the action that might have been in flight: `awaiting_decision` →
  `needs_input`; an `external` tool intent with no outcome — no outcome row with its
  `operationId` and not in `recorded` (operation ids the `operations` table has) — →
  `unrecoverable` (it may or may not have happened; a person checks); otherwise →
  `resumable`. There is no `restartable` any more. `open_external_intents()` lists those
  actions; `has_external_effect()` (any `external` row at all) is what makes a restart need
  `force`.
- `diagnose_stall(tail)` — first match wins over the last `DIAGNOSE_TAIL_SIZE` (8) trace rows,
  counting only rows after the last `nudge` (the pattern a retry already answered):
  `exact_repeat`, `oscillation`, `repeated_failure`, `near_duplicate_reasoning`
  (`NEAR_DUPLICATE_SIMILARITY` 0.85). Returns `{cause, detail}` or `None`.
- `is_hung`, `has_capacity`, `resource_available`, `step_budget_exceeded`
  (`STEP_BUDGET_BY_KIND`) and `can_auto_retry` — **one automatic attempt, total**: a crash
  retry and a stall retry share the same `retries` counter, so a job cannot get two goes by
  failing two ways.

## `worker.py` — drives one job, as durable work

**A job is durable work (`jarvis/durable.py`, kind `job`).** `run_job(job_id, answer=, fresh=)`
is the one call for every path — start, resume after a park, the supervisor's retry, recovery
after a restart, restart (`fresh=True`) — and `durable.advance` knows which. The job runs in
**rounds**, each an ordinary turn (`_turn`) on its own session `job:<id>` (`session_for()`),
with a replay-stable `operation_scope` and `continuable=True`: a round that uses its last step
writes a progress note and the next round continues from the saved transcript. Rounds continue
until the model answers before its last step, or the job's model steps reach
`STEP_BUDGET_BY_KIND` (now really applied, and never overshot — the last round is cut to what
is left), when it parks with "still not done after N steps — keep going?" (`reason: budget`).
A finished round is never run again; a round cut off by a crash is run again from its start,
and every tool call it had finished returns its recorded result instead of running twice.
`answer=None` (a plain start or recovery) never answers a waiting job on the person's behalf.

**A worker structurally cannot write into the conversation the user is looking at**: its
session is keyed separately and never bound to chat history. **A worker never asks the user
directly**: it runs with `Autonomy.PRE_CONSENTED` on `Surface.JOB` — the person set the work
going, so MEDIUM steps toward its goal run (the same "set up in advance" rule as a scheduled
task; Phase 3) — and a HIGH-risk call needing a human parks
the work (`_park` → `_park_for_approval`) — status `awaiting_decision`, a `decision` trace row
and a Tier 1 outbox row — and the durable work waits. A job may wait hours, so the record is a
table row, not a short-lived token. Every hook the runner calls is safe to replay: outbox rows,
the decision row and the finish note are written once (`_outbox_once`, per attempt for the
finish).

- **Tools by kind.** `TOOLS_BY_KIND`: `generic` is `None` (the full catalogue, no fence);
  `research` and `files` are small hardcoded lists plus `JOB_OWN_TOOLS` (`request_job_split`).
  Every restricted kind also gets every installed Skill (`_installed_skill_names()`), since a
  Skill is the user's own packaged process rather than a raw capability the fence exists to
  restrict. `computer` never goes through this loop: it is admitted parked (below). `kind` ONLY
  gates which tools are callable — there is no per-kind system prompt, and the worker's first
  message is the raw `goal`.
- **Tracing.** `_observe` writes the intent/outcome rows (above); each `ToolRan` refreshes the
  heartbeat. A failed round (no model, an error) parks the work as `stalled` (`_stall`) for
  the supervisor.
- **Completion is verified** (`_finish`, the kind's finisher). "It finished" is not "it did what
  was asked": `_verify_result()` calls `ops/verify.py`'s `verify_semantic_match()` with the
  job's goal. A checked mismatch is refused (`accepted: False`) and treated exactly like a
  stall — the same single retry, the same counter, the same escalation, never a second recovery
  mechanism. `checked: false` (no model available) never blocks a real completion. Success
  stores the result, publishes `JOB_COMPLETED` and delivers it (`_deliver_result`): a **tier-2**
  notice carrying the whole result (`detail.result`, `conversationId`, `files`, `attempt`,
  `announced`) that `prompt.notices_section` shows on the next person-started turn (scoped to the
  conversation that asked; `acknowledge_notice` passes it on), a notification, and — when
  `heartbeat/speak.may_speak_now()` says they are here — Jarvis speaking first (≤
  `SPOKEN_RESULT_CHARS` whole and the row marked delivered; longer: a headline, full result waits
  with `announced: true`). Questions a job cannot go on without are asked aloud the same way
  (`_ask_aloud`: go-ahead, out of steps, stuck, crashed). Delivery is replay-safe (`_outbox_once`
  returns None for a row that exists, ignoring `announced`) and a failure to speak or notify never
  fails the job.

## `orchestrator.py` — admission, supervision, crash recovery

- `admit(title, goal, kind, priority, conversation_id, parent_id)` — the ONE capacity-gated
  creation path, used by `routes/jobs.py` and `tools/job_tools.py`, so they cannot diverge on
  what "at capacity" means. Capacity (`MAX_ACTIVE_JOBS` 3, overridable by
  `prefs.maxBackgroundJobs` via `active_job_limit()`) is checked BEFORE anything is spent, and
  past it `AtCapacity` is raised so the user is told and asked what should give way; nothing is
  silently queued. `RESOURCE_BY_KIND` makes `computer` jobs hold one exclusive resource.
  **A `computer` job never starts unattended:** it is created `awaiting_decision` with a Tier 1
  outbox row asking to start.
- `resume(job_id, guidance)` — the one way a parked job starts again, for every kind of park.
  It continues from the last finished round; guidance (a `nudge` trace row) is what the next
  round is told. It marks the outbox rows delivered because this action is what actually
  resolves the decision.
- `answer_approval(job_id, approval_id, allowed, result)` — called by `routes/approvals.py`
  when the person answers an approval a job (`job:` session) is waiting on: the job's saved
  transcript gets the real result (or "declined") in place of "needs your go-ahead", and the job
  carries on by itself. Nothing happens unless it is waiting on exactly that approval.
- `restart(job_id)` — stops it, clears the result, and starts over from the goal
  (`durable.forget` → a new epoch, so finished actions are genuinely done again).
- `cancel(job_id)` — works from any status, stops the round in flight (`durable.stop`, the
  turn's cancel event) and marks pending outbox rows delivered, so a cancelled job's old
  question never resurfaces. No later round starts (`worker._stopped`); "keep going" continues it.
- `supervise()` — the periodic pass (`TICK_SECONDS` 60, gated by `JARVIS_JOBS`). A `running`
  job with no heartbeat for `HANG_TIMEOUT_MS` (5 min) is hung; otherwise `diagnose_stall()` runs
  on its trace tail. `_recover()` stops a live one first (never two runs at once — the durable
  per-work lock also guarantees it), then spends the job's one retry — a RESUME from its last
  round, told why (`note`) — or, if it is spent, parks the job `awaiting_decision` with a Tier 1
  `stuck` outbox row.
- `recover_orphans()` — at startup: `queued` jobs start; anything still `running` crashed (no
  heuristic needed). The trace and the durable runner's own write-ahead record
  (`durable.unsure`) decide whether continuing is safe: `resumable` jobs continue from their
  last finished round; one that died inside an external action is parked with a Tier 1
  `crashed` row naming it. The crash is also recorded for Self-Improvement.
- `start()` / `stop()` run the timer. `start()` first runs the specialist-run recovery
  (`agents/durable_runs.recover_at_startup`, which builds the registry and so closes orphaned
  runs off) BEFORE recovering jobs, so a recovered job's new run is never closed as an orphan.

## Routes and tools

`routes/jobs.py` exposes list/create/get plus `POST /jobs/{id}/resume`, `/restart` and
`/discard`. `restart` is REFUSED when anything the job did or started reached outside Jarvis
(`has_external_effect` on its trace) unless the caller passes `force`, because starting over
repeats it, and repeating something that reached the outside world is not something a retry
can undo. (`resume` never repeats a finished action, so it needs no such check.) `tools/job_tools.py` provides `work_in_background`, `check_on_work` and
`stop_working_on`.

## Splitting (`tools/job_split.py`, `request_job_split`)

Declared only on a job's own turn (the `job` tag, filtered in `orchestrator/pipeline.py`).
**A split is judged, never automatic**: cheap code-only checks first (piece count, room for all
of them), then one model call judging whether the split is proportionate. No model, an
unreadable answer and uncertainty all mean "keep it as one job". **The depth ceiling is
structural**: every piece takes the root ancestor as its parent, resolved in one hop, so pieces
become PEERS under the same root and a tree cannot exceed depth 2. Approved pieces are always
`kind: generic`, so a split costs one judgment call however many pieces it produces, at the
price of a piece never getting a specialised kind.
