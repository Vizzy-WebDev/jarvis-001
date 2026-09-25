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
  wrappers over `ops/trace.py` with `source='job'` bound. A caller writes an `intent` row
  (`effect`: `read` | `workspace` | `external`) BEFORE an effectful action and an `outcome` row
  after, so a crash between the two still leaves the intent on record. That is what lets
  `policy.classify_recovery()` derive an honest verdict instead of a job assessing itself.
- **The Tier 1/2/3 interruption queue** — `add_outbox()`, `list_pending_outbox()`,
  `mark_delivered()` and `deliver_all_for_job()` are thin wrappers over `heartbeat/outbox.py`
  with `source='job'` bound. `orchestrator/context.py` drains pending rows into the next turn
  the user starts. `reason` is `permission` (a parked approval, or a desktop-control job asking
  to start), `stuck` (a stall or hang that survived its one retry) or `crashed`.

## `policy.py` — pure functions, zero imports

Run on every supervisor tick for every active job, so: no model calls, no side effects, no I/O,
and checkable as a truth table (the same discipline as `memory/policy.py`).
- `classify_recovery(job, trace)` — a job found `running` at startup with nothing running it.
  Pessimistic: `awaiting_decision` → `needs_input`; ANY `external` trace row (even a bare
  intent, since a crash between logging and doing is indistinguishable from one after) →
  `unrecoverable`; no `workspace` rows → `resumable`; otherwise → `restartable`.
- `diagnose_stall(tail)` — first match wins over the last `DIAGNOSE_TAIL_SIZE` (8) trace rows:
  `exact_repeat`, `oscillation`, `repeated_failure`, `near_duplicate_reasoning`
  (`NEAR_DUPLICATE_SIMILARITY` 0.85). Returns `{cause, detail}` or `None`.
- `is_hung`, `has_capacity`, `resource_available`, `step_budget_exceeded`
  (`STEP_BUDGET_BY_KIND`) and `can_auto_retry` — **one automatic attempt, total**: a crash
  retry and a stall retry share the same `retries` counter, so a job cannot get two goes by
  failing two ways.

## `worker.py` — drives one job

`run_job(job_id)` runs the job's goal as a real turn on its own session, `job:<id>`
(`session_for()`). **A worker structurally cannot write into the conversation the user is
looking at**: its session is keyed separately and never bound to chat history. **A worker
never asks the user directly**: it runs with `Autonomy.ESCALATE` on `Surface.JOB`, so a call
needing a human is parked (`_park()`) — status `awaiting_decision`, a trace row and a Tier 1
outbox row. A job may wait hours, so the record is a table row, not a short-lived token.

- **Tools by kind.** `TOOLS_BY_KIND`: `generic` is `None` (the full catalogue, no fence);
  `research` and `files` are small hardcoded lists plus `JOB_OWN_TOOLS` (`request_job_split`).
  Every restricted kind also gets every installed Skill (`_installed_skill_names()`), since a
  Skill is the user's own packaged process rather than a raw capability the fence exists to
  restrict. `computer` never goes through this loop: it is admitted parked (below). `kind` ONLY
  gates which tools are callable — there is no per-kind system prompt, and the worker's first
  message is the raw `goal`.
- **Tracing.** Each `ToolRan` writes an outcome row and refreshes the heartbeat. A stall found
  in the trace tail with no answer goes to `_stall()`.
- **Completion is verified.** "It finished" is not "it did what was asked":
  `_verify_result()` calls `ops/verify.py`'s `verify_semantic_match()` with the job's goal. A
  checked mismatch is treated exactly like a stall — the same single retry, the same counter,
  the same escalation, never a second recovery mechanism. `checked: false` (no model available)
  never blocks a real completion. Success stores the result, publishes `JOB_COMPLETED` and adds
  a Tier 3 outbox row (worth recording, never worth interrupting for).

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
  It marks the outbox rows delivered because this action is what actually resolves the decision.
- `cancel(job_id)` — works from any status and marks pending outbox rows delivered, so a
  cancelled job's old question never resurfaces.
- `supervise()` — the periodic pass (`TICK_SECONDS` 60, gated by `JARVIS_JOBS`). A `running`
  job with no heartbeat for `HANG_TIMEOUT_MS` (5 min) is hung; otherwise `diagnose_stall()` runs
  on its trace tail. `_recover()` spends the job's one retry (back to `queued`, with a trace
  note) or, if it is spent, parks the job `awaiting_decision` with a Tier 1 `stuck` outbox row.
- `recover_orphans()` — at startup anything still `running` crashed (no heuristic needed). The
  trace decides whether picking it back up is safe: `resumable` and `restartable` jobs are
  requeued, and anything else is parked with a Tier 1 `crashed` row. The crash is also recorded
  for Self-Improvement.
- `start()` / `stop()` run the timer.

## Routes and tools

`routes/jobs.py` exposes list/create/get plus `POST /jobs/{id}/resume`, `/restart` and
`/discard`. `restart` is REFUSED for a job the trace marks `unrecoverable` unless the caller
passes `force`, because repeating something that reached the outside world is not something a
retry can undo. `tools/job_tools.py` provides `work_in_background`, `check_on_work` and
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
