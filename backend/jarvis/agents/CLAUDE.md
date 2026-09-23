# Specialist agents (`jarvis/agents/`)

Jarvis is the orchestrator; the model is the brain; a **specialist agent** is a domain expert
Jarvis hands work to. Tools, Skills, connectors and MCP are *capabilities* — hands any agent
may be given — never agents themselves. Boundaries are by responsibility, not by tool.

## One kind of agent

- `store.py` (leaf over `db.py`, module `RLock`): `agents`, `agent_runs`, `agent_notes`
  (migration 30). **A built-in agent is a row seeded from `builtins.py`; a custom one is a
  row the person made.** `builtin` only decides reset (built-in) vs delete (custom). Nothing
  that RUNS an agent reads it — do not add a second path for custom agents.
- `builtins.py` — the 13 defaults as plain data (`research, strategy, content,
  creative-media, video-production, marketing, advertising, sales, commerce, operations,
  analytics, teacher, scout`) plus `COMMON_GUARDRAILS`. Bump an entry's `version` when its
  text changes: `seed_builtins` refreshes only rows the person never edited
  (`createdAt == updatedAt`). Scout has no category list, on purpose — its target comes from
  the operator's words or their Memory.
- `ensure_builtins()` (`__init__.py`) is cheap and called by every reader that needs the roster.

## Running one — `runner.py`

An agent run is an ordinary `Orchestrator.run_turn` with three things set on the
`TurnRequest`: `agent=AgentBrief` (identity/doctrine/guardrails → `prompt.specialist_instruction`
via `RelevanceContext`), `allowed_names` (its access, enforced by `policy/decide.py`) plus
`always_declare` (what is declared up front — a broad allowlist stays reachable through
`find_capability` instead of blowing the 20-tool budget), and `model_id` (the same
found-or-refused pin a scheduled task uses; null = Jarvis's selection, Auto included).
**The turn loop never imports this package** (`test_architecture.py`).

- Sessions: `agent:<agentId>:<conversationId>` (relay), `job:<id>` (background job),
  the person's own conversation (talking directly, `GET /api/chat/stream?agent=`). The
  session persists per conversation, and `_reseed` puts back the last few task/result pairs
  after a restart.
- Every agent also gets `find_capability`, `read_my_notes`, `write_my_note`, `ask_specialist`
  (only if it has collaborators) and all installed Skills. Specialists always see the Memory
  categories `Long-term Goals` and `Projects` (`context.SPECIALIST_FLOOR_CATEGORIES`), if
  their memory access is on.
- Threads: a run's turn happens on an `agent-run-…` thread, and `capabilities/execute.py`
  gives those threads their own pool, so a delegation waiting on a nested turn can never
  starve it. `test_many_delegation_chains_at_once_all_finish` guards this.

## Delegation — `capabilities.py` + `runner.delegate()`

`ask_specialist` is the one delegation primitive, for Jarvis and agents alike, registered from
`assembly.py` after the loader (like Skills and connectors) and **re-synced on every agent
change** so its roster is current on the next turn. The guards are code, not prompt: only the
asker's `collaborators`, no cycles, depth ≤ `MAX_DEPTH` (3), ≤ `MAX_RUNS_PER_ROOT` (8) runs
per request. The asker is found from the calling turn's session (`running_run_for_session`).

- **An approval inside a specialist stops its run** and comes back as plain data;
  `pipeline._approval_of` turns it into a real `ApprovalRequired` card. No agent answers one.
- **Files** a run's tools make are handed up (`ui_action` `attachments`) so they reach the chat.
- **Slow specialists are never cut off.** Jarvis's turn waits `DELEGATION_WAIT_S` (4 min); a
  run still going is detached, Jarvis is told "still working, do not ask again", and on
  finishing `_deliver_late` sends a late `agent.run_finished` event (the open chat shows the
  result), a notification, and a `source='agent'` outbox notice carrying the full result that
  `prompt.notices_section` shows Jarvis on the next message. Measured live: free-tier models
  took 1–3 min per specialist step; without this the chat sat silent for 15 min and gave up.
  An agent asking another still waits fully — its own deliverable needs the answer.
- `background=true` admits a job with `agent_id`; `jobs/worker.py` runs it through
  `stream_run`, so supervision/retry/parking are the jobs system's.
- A scheduled `prompt` task may carry `agentId` (`schedule_task`'s `agent`); every run of
  one task shares `task:<id>` as its conversation, so a standing hunt carries on.

## Verifying what an agent actually did

Read `agent_runs` (who asked, status, result, `tools_used`, `model_id`, the tree via
`root_run_id`/`parent_run_id`) and the `operations` table — never a model's own account.
`GET /api/agent-runs/{id}` returns the whole tree. Tests drive real turns with
`tests/session_scripted_model.py` (one script per speaker, identified from the specialist's
own prompt line, so job and direct-chat sessions work too).
