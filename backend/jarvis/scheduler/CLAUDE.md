# Scheduler + briefing (`jarvis/scheduler/`)

- `recurrence.py` — pure `next_run_at(spec, start)` / `describe(spec)` over
  `{type: 'once'|'daily'|'weekdays'|'weekly'|'interval', ...}`. No state, no I/O — test it
  directly.
- `task_store.py` — plain CRUD over `data/tasks.json` / `data/task-runs.json`. Deliberately
  a leaf module, so the engine and the briefing composer can both read tasks without
  importing each other. Keeps the last `MAX_RUNS_KEPT` runs.
- `engine.py` — the 30s tick (`tick()`) and `run_task_now()`. Gated by `JARVIS_SCHEDULER`:
  a second process reading the same `tasks.json` would fire every task twice, so the
  interlock is a real safety property, not just a test guard.
  - **Action types:** `message` (fixed text, no model), `prompt` (free text run as a real
    turn), `briefing` (`compose_briefing()`). Anything else fails with "Unknown task action".
  - **The schedule advances BEFORE the run**, so a task that throws, or a restart
    mid-run, can never re-fire the same due timestamp forever. A run missed for days
    catches up exactly once and is flagged `late` (more than `LATE_THRESHOLD` past due).
  - **A `prompt` task runs as its own ephemeral session** (`task:<id>:<suffix>`), never
    bound to chat history, with `Autonomy.PRE_CONSENTED` on `Surface.SCHEDULED`. Pre-consent
    covers ordinary work only — a HIGH-risk call inside it still parks for a human
    (`policy/decide.py` enforces that, not this module). A parked approval is recorded as
    `awaitingApproval`, neither a success nor a failure. `action.modelId` is a pin passed to the
    model client; nothing honours it while there is no model system.
  - **Connectors on a prompt task** (`action.connectors`, connector ids from the task
    UI's picker) are resolved to tool names at RUN time via
    `connectors/capabilities.py`'s `tool_names_for()`, because a connector's tool list
    changes when it reconnects. Picking connectors ADDS them to the non-connector
    capabilities; unpicked connectors are excluded. No connectors means no restriction.
    `action.tools` (explicit names) overrides both.
  - **Verification.** For a `prompt` run that reports ok, `_verify_run()` calls
    `ops/verify.py`'s `verify_semantic_match()`. A `matches: false` verdict flips the run
    to not-ok with the reason folded into `error` — no new recovery mechanism, since the
    task's next occurrence is its natural retry. Only `prompt` runs get this; `message` and
    `briefing` results are mechanical. `checked: false` (no model available) never turns a
    real success into a failure.
  - **After a run:** the outcome is recorded via `observers/improvement.py`; a
    `JOB_COMPLETED` event always fires so open screens see it; a notification fires only if
    the task's own `notify` setting says so; and a successful `prompt` result is
    checkpointed to memory in the background.
- `briefing_config.py` / `briefing.py` — the config is split from the composer so a tool
  can read and write it without importing the model layer. Sections (greeting, date/time,
  tasks, goals, focus, custom) are fixed. Weather and headlines are fixed native
  abilities, not a user-managed "sources" list — a list would offer Jarvis's own built-ins
  through the same "add a source" UI as a Skill. Connectors (`config.connectors`, ids,
  empty by default) are the one real user-picked addition.
  - **The model narrates, it never supplies.** `compose_briefing()` gathers every fact in
    code first (`gather_facts()`) and the prompt says anything not listed is unknown. With
    no connector picked the narration turn has no tools at all. With connectors picked it
    also gets real access to exactly those connectors' tools (`connector_tool_names()`,
    resolved at compose time), and nothing else.
