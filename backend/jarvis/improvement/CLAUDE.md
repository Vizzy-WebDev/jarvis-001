# Self-Improvement (`jarvis/improvement/`)

See the root `CLAUDE.md`'s "Self-Improvement" section for the decisions that matter beyond
this file (the lessons-vs-rules split, the undo refuse-vs-clobber design, the budget ledger, the
domain-exclusion guard). This file is the module-by-module breakdown. Self-Improvement is about
Jarvis's OWN work; Memory is about the user.

## The chain

Four separate things, on purpose, so that noticing something once can never become a permanent
change to how Jarvis behaves:

1. **Outcome** (`improvement_outcomes`) — a fact: something happened. Written by `capture.py`
   (zero model calls) the instant a job or task finishes, a job crashes, a notable tool call
   fails or is refused or parked, or a plainly-worded correction fires.
2. **Lesson** (`improvement_lessons`) — an OBSERVATION. `reflect.py` (one batched model call)
   turns a backlog of outcomes into lessons. A lesson never changes behaviour by itself.
3. **Proposal** (`improvement_proposals`) — a candidate CHANGE. `synthesize.py` (one rarer
   call) looks across the active lessons for something that genuinely recurred. A `rule`
   proposal is only created when at least `MIN_LESSONS` (2) lessons backed by at least
   `MIN_DISTINCT_OUTCOMES` (2) DISTINCT underlying outcomes agree; two lessons from one event are
   one event described twice. `tools/improvement_tools.py` (`suggest_improvement`) can create any
   kind, but always with `evidence: []`, which the evidence floor routes to always-ask.
4. **Rule** (`improvement_rules`) — the only thing ever injected into the system prompt.
   `apply.py` is the only thing that creates one. `improvement_changes` is the append-only
   audit/undo log: one row per applied change, and one MORE row (`kind: 'undo'`) per undo, never
   a rewrite of the original.

Other modules: `store.py` (the leaf over all of this plus the spend ledger), `policy.py` (the
gate), `domains.py` (the exclusion guard), `cadence.py` (the clock), and
`implementation_prompt.py` (on demand only, not part of the background cycle).

**`synthesize.py` computes a proposal's `sourceTier` from the WORST (highest-numbered) tier among
its supporting lessons**, never a hardcoded 1. A pattern leaning on even one lesson read from
outside must not look tier-1 to `decide()`, or an outside-sourced idea could slip past the
"outside ideas always ask" floor by riding along with genuinely observed evidence.

**`reflect.py` marks a batch of outcomes reviewed ONLY when the model call actually succeeded.**
Whenever no model is available, marking them on a failed call would
permanently discard real learning material. A genuine attempt (a parseable reply, even an empty
`lessons` array) earns "reviewed"; a failed one leaves them for the next tick. The daily budget
unit is still spent on a failed attempt — that is the brake against hammering retries while every
model is down.

## Import discipline

| Module | Imports | Why |
|---|---|---|
| `store.py` | **leaf** — `db.py` only | Safe for `tools/*.py` and `prompt.py` to import directly |
| `policy.py` | **pure** — `prefs.py` only | A truth table, like `memory/policy.py` |
| `domains.py` | **zero imports** | A floor, directly testable |
| `capture.py` | `store.py`, `jobs/job_store.py` — both leaves | Zero model calls, so cheap enough to call from anywhere |
| `apply.py` | `store.py`, `prefs.py` | Never imports anything that could write to the source tree |
| `reflect.py`, `synthesize.py` | + `ai.py` (the one seam for asking a model) | The only modules here that spend a model call |

## How capture is wired

`orchestrator/pipeline.py` deliberately imports nothing from `jarvis.improvement` (a fitness test,
`test_the_turn_loop_imports_no_subsystem_that_watches_it`, enforces this), so outcomes arrive by
other routes:
- **Job completion and correction** — `observers/improvement.py` subscribes to the event bus.
  `JOB_COMPLETED` / `JOB_UPDATED` re-read the job and its trace at event time (never trusting the
  event's thin payload) and call `capture.record_job_outcome()`. `ASSISTANT_INPUT` carries the
  turn's raw text and feeds `note_correction()`, skipped on a background turn.
  `record_job_outcome()` is safe to call on every update and twice for one job: it checks
  `TERMINAL_JOB_STATUSES` and `improvement_outcomes` is `UNIQUE(source, source_ref)`.
- **A split completion is never an outcome.** `tools/job_split.py` marks the original job `done`
  with a "Split into N separate job(s)" result, which is a division of work, not a finished piece
  of it. `record_job_outcome()` excludes it by the result prefix and, more robustly, by whether
  the job actually has children.
- **A scheduled task run** — `scheduler/engine.py`'s `run_task_now()` calls
  `observers/improvement.py`'s `_record_task_outcome()` directly (a task run has no event of its
  own). It is captured on EVERY run and outcome, `ok` or not, since capture costs nothing.
- **A job crash** — `jobs/orchestrator.py`'s `recover_orphans()` calls `_record_job_crash()`
  directly (a crash has no event either). It writes under `source: "job_crash"` with a fresh
  `source_ref`, so it can never collide with `record_job_outcome()`'s per-job dedup key.
- **A notable tool call** (a real failure, a refused call, a parked approval) —
  `observers/recording.py`'s `_record_notable_tool_outcome()` subscribes to `TOOL_FAILED`,
  `TOOL_REFUSED` and `TOOL_ESCALATED` and calls `capture.record_notable_tool_outcome()`.

**`source_ref` and `entity_ref` are two different columns on purpose.** `source_ref` is the
per-EVENT dedup key (a job's id, one scheduled RUN's id). `entity_ref` is the stable key for the
RECURRING thing (a job's id again, or a scheduled TASK's own id), so `reflect.py` and
`synthesize.py` can group "this specific task tends to fail this way".

## `policy.py` — the one gate

`decide()` is asked by every path: the background cycle, the screen's Approve button and a
conversational suggestion. Floors, in this order, none overridable by trust level: the `kind`
must be a `rule` or a `setting` (`APPLIABLE_KINDS`; a Skill, code change or idea always asks —
Jarvis never edits itself); `sourceTier` must be exactly 1 (only Jarvis's own directly-observed
history, never anything read); `conflictWith` must be unset. Only past all three does trust
matter: `MIN_EVIDENCE_BY_TRUST` (`ask`: infinity, `balanced`: 2, `auto`: 1) is a floor BENEATH,
not instead of, `synthesize.py`'s own code-level check.

## The budget ledger

`store.py`'s `try_consume_daily_budget()` and `try_consume_weekly_budget()`, backed by the
`app_state` key/value table, are a real spend cap: `DAILY_BUDGET` 2 is shared by `reflect` and
`synthesize`, and each has its own cadence floor (`reflect` 4 hours, `synthesize` 24 hours).
`cadence.py` ticks every `TICK_SECONDS` (900), gated by `JARVIS_IMPROVEMENT`, and is thin on
purpose — `reflect()` and `synthesize()` gate themselves on enough new material, their cadence
and the budget, so the tick just has to run often enough for those gates to say yes.
`WEEKLY_BUDGET` (4) and its counter exist and are reported by `GET /improvement/status`, but
nothing in this package currently consumes it.

## `domains.py` — the exclusion guard

`is_excluded()` keeps subjects Jarvis must never proactively reason about (a person's emotional
life and relationships) out of every pattern-finding call. It is deliberately biased BROAD, the
opposite of the tone floors in `personality.py`: a false NEGATIVE means excluded content reaches a
model call the user said must never happen, while a false positive merely drops one item. It is
applied on BOTH sides in `reflect.py` and `synthesize.py` — matching material is filtered out of
the input before the model sees it, and any produced insight that still matches is dropped after —
and never left to a prompt instruction alone. Every relationship pattern is anchored to an actual
personal-relationship noun, so "a good relationship with this codebase" does not match.

## Undo — refuse, never clobber

`apply.py`'s `undo_change()` compares the LIVE value against what the change actually set
(`after`, not just `before`). If they differ — the user muted the rule or edited the pref since —
it refuses with `{ok: false, reason: 'changed_since', current, expected}` (`UndoRefused`) rather
than overwrite their later decision; `force: true` proceeds only after the caller has shown the
user the mismatch. An `undo` row is itself never undoable: bringing something back means
approving a fresh proposal, which keeps every `before`/`after` pair honest.

## The screen (`frontend/components/screens/ImprovementScreen.tsx`, `#/improvement`)

Four tabs: **Suggestions** (pending proposals, with a "Show rejected" toggle; `skill` and `code`
kinds get "Generate a prompt for a coding assistant" instead of Approve, since generating that
brief IS the approval for those kinds), **Changes** (the undo log, with titles resolved from a
change's own stored `after` snapshot, so a since-deleted rule still displays), **Learned** (rules
with a mute toggle, and lessons, each with a "Show archived" toggle) and **Settings** (the trust
dial, the outside-research toggle, the budget counters). Filtering is client-side over small
curated lists. Every row opens a detail view; a rule's detail is the one place its text is
editable, and Save writes a real `improvement_changes` row through the same undo machinery.
Archive is soft; permanent delete is reachable only from an already-archived or rejected item,
because a rule's Undo button depends on the row still existing.

**A detail modal's terminal actions must close it themselves.** Archive, Restore, Delete, Approve and
Reject in `ImprovementScreen.tsx`'s detail views must call the modal's `onClose` after the action
completes. Forgetting it leaves the modal and its scrim open over a correctly-refreshed list, which looks
exactly like a frozen page.

## Tools (`jarvis/tools/improvement_tools.py`)

`record_lesson` (`core`, `meta` — the user just plainly taught Jarvis something; files an
`explicit` outcome via `record_explicit_teaching()` first, so the lesson has a real outcome to
cite, then goes through the SAME synthesis gate as any other, with no bypass),
`review_improvements` (`core`, `meta` — "what have you learned/changed"), `suggest_improvement`
(non-core; always `evidence: []`, so `decide()` always asks) and `undo_improvement` (non-core, no
confirmation — undo is the safe direction; a refused undo is surfaced plainly).

**A tool's `description` alone is not enough to make the model call it.** `prompt.py`'s system
instruction needs an explicit paragraph saying WHEN to use each tool (see the `record_lesson` and
`suggest_improvement` lines there, following the Memory section's pattern). Without it, asking
Jarvis to teach it something produced a reasonable-sounding reply that filed nothing. Any future
tool added here needs the same explicit trigger wording, verified with a stub model that captures
whether a `tool_calls` array was actually sent.
