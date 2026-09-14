<!-- Ported from the Node build during the S6 cutover. The architecture, the
invariants and the live-caught bugs described here all carried over deliberately and
still hold. File paths have been updated to their real Python counterparts and are
verified to exist. Function names written in camelCase (`getToolDeclarations()`) are
the NODE originals, kept because the surrounding reasoning is about them; the Python
equivalent is the snake_case function doing that job in the same module. Where a Node
module had no Python counterpart, the text says so rather than pointing at a file that
does not exist. -->

# Self-Improvement (`jarvis/improvement/*.py`)

See the root `CLAUDE.md`'s "Self-Improvement" section for the decisions that matter
beyond this file (the lessons-vs-rules split, the undo refuse-vs-clobber design, the
budget ledger, the domain-exclusion guard). This file is the module-by-module
breakdown.

## The chain, and why it's five modules instead of one

`capture.py` (0 model calls) writes `improvement_outcomes` rows the instant a job/task
finishes or a plainly-worded correction fires. `reflect.py` (1 batched call) turns a
backlog of outcomes into individual `improvement_lessons` — observations, not yet
behaviour changes. `synthesize.py` (1 rarer call) looks ACROSS the whole active lesson
set for something that genuinely recurred and turns THAT into an `improvement_proposals`
row — a `rule`/`setting` proposal only ever gets created here when at least two
DISTINCT underlying outcomes support it (`MIN_SUPPORTING_OUTCOMES`), never from one
lesson however clearly phrased. `improvement/policy.py`'s `decide()` is the one seam
every path (auto-apply, the screen's Approve button, a conversational
`suggest_improvement` call) asks before anything actually changes; `apply.py` is the
only thing that ever writes a real rule or flips a real pref, and the only thing that
ever undoes one.

**`reflect.py` only marks a batch of outcomes reviewed when the model call actually
succeeded — a real, previously-latent bug, found live in Phase 7 by re-running the OLD
Phase 1-6 regression scripts rather than assuming they'd still pass.** The original
version called `markOutcomesReviewed()` unconditionally, even when `askModel()` came
back `{ok:false}` (no model available, or none produced parseable JSON). On this user's
own real model roster — routinely ALL rate-limited at once, a documented, expected
state for this project, not an edge case (see root CLAUDE.md's Gotchas) — that would
have permanently discarded real learning material on nothing more than "quota was tight
this exact tick," since `pruneReviewedOutcomes()` would eventually delete the row too.
Only a genuine attempt (the model replied with something parseable, even an empty
`lessons` array) earns marking outcomes reviewed now; a failed attempt leaves them
exactly as they were so the next tick, once a model is healthy again, gets a real shot
at them. The daily budget unit is still spent on a failed attempt either way — that's
the actual brake against hammering retries back to back while every model is down, not
something this fix removed.

Two more model-calling modules, both weekly-budgeted separately from the daily
reflect/synthesize budget: `improvement/reflect.py` (tiers 2-4 — official docs,
communities, general web, reusing `jarvis/research.py`'s own free-web-then-model-search
path rather than reimplementing it) and `improvement/synthesize.py` (patterns in the user's OWN
work/projects/study/finances/travel, never emotional state or relationships). Both feed
the SAME lesson/proposal pipeline above — there is exactly one place a lesson becomes a
proposal (`synthesize.py`), never a second path for outside-sourced material.

`improvement/implementation_prompt.py` is a sixth, on-demand-only module (not part of the
background cycle at all) — turns an approved `skill`/`code` proposal into a brief for
whichever coding assistant the user names, never reading the repo itself.

## Import discipline — why the split is exactly this shape

| Module | Imports | Why |
|---|---|---|
| `improvement/store.py` | **leaf** — `db.py` only | Safe for `jarvis/tools/*.py` and `prompt.py` to import directly, same reasoning as `memory/memory/store.py` and `jobs/job_store.py` |
| `improvement/policy.py` | **pure** — `prefs.py` only | Testable with a bare `python -c` truth table, same as `memory/policy.py` |
| `domains.py` | **zero imports** | Same reasoning as the Node build's style floors (not ported — see the root CLAUDE.md) — a floor, directly testable, no server needed |
| `capture.py` | `improvement/store.py`, `jobs/job_store.py`, both leaves | Zero model calls, ever — cheap enough to call from anywhere with no latency risk. Only `scheduler/engine.py` calls it directly; `orchestrator/pipeline.py` never imports it at all (see below) |
| `apply.py` | `improvement/store.py`, `prefs.py` | Never imports anything that could write to the repo's own source tree — see its own header comment on why that's the one invariant this whole module exists to protect |
| `reflect.py`, `synthesize.py`, `improvement/reflect.py`, `improvement/synthesize.py`, `improvement/implementation_prompt.py` | + `ai.py` (and, for `improvement/reflect.py`, `research.py`) | Not leaf — safe to import from `improvement/reflect.py` only |
| `improvement/reflect.py` | everything above | The only non-leaf top-level module; `startImprovementCycle()` called once from `main.py`, beside `startScheduler()`/`startOrchestrator()` |

**Three different wiring shapes reach `capture.py`, not one.** `scheduler/engine.py`
calls `record_task_outcome()` directly — a scheduled task run has no event of its own
to subscribe to, and the call site already has both `run` and `task` in hand right
where `record_run()` builds them. `jobs/orchestrator.py`'s `recover_orphans()` calls
`observers/improvement.py`'s `_record_job_crash()` directly the same way, for the
same reason — a crash has no event of its own either. Ordinary job completions and
corrections do NOT reach `capture.py` either of those ways: `orchestrator/pipeline.py`'s
own header states it deliberately never imports `jarvis.improvement` at all
(`test_architecture.py`'s `test_the_turn_loop_imports_no_subsystem_that_watches_it`
enforces this), so `observers/improvement.py` subscribes to the event bus instead —
`JOB_COMPLETED`/`JOB_UPDATED` (re-reading the job and its trace at event time rather
than trusting the event's own thin payload) feed `record_job_outcome()`, and
`ASSISTANT_INPUT` (which now carries the turn's raw `text`, added specifically for
this) feeds `note_correction()`, gated on the turn not being `background`. A tool
call notable enough on its own — a real failure, a refused-allowlist call, a parked
confirmation — reaches it a fourth way: `observers/recording.py`'s
`_record_notable_tool_outcome()` subscribes to `TOOL_FAILED`/`TOOL_REFUSED`/
`TOOL_ESCALATED` and calls `record_notable_tool_outcome()`, separate from that same
file's `_record_tool_outcome()`, which only ever feeds the Self-Model tally (see
`self/CLAUDE.md`). Whichever way it arrives, `capture.py` itself stays a leaf three
hops from any model call — the wiring differs, the zero-cost property doesn't.

## The hook points — verified against the real code, not assumed

A job's terminal status (`done`/`stalled`/`cancelled`/`failed`) is caught through the
event bus: `worker.py`'s done/stalled paths and `orchestrator.py`'s `cancel()` publish
`EventType.JOB_COMPLETED`/`JOB_UPDATED`, and `observers/improvement.py` subscribes to
both, re-reading the job and its trace at event time (never trusting the event's own
thin `{id, status, title}` payload) before calling `capture.record_job_outcome()`.
That function's own `TERMINAL_JOB_STATUSES` guard plus `improvement_outcomes.UNIQUE(source,
source_ref)` (`INSERT OR IGNORE` under the hood) make it safe to call on every
`JOB_UPDATED`, not just a terminal one, and safe to call twice for the same job.

**A crash-classified orphan IS captured as an outcome, distinct from the job's own
eventual terminal status.** `jobs/orchestrator.py`'s `recover_orphans()` calls
`observers/improvement.py`'s `_record_job_crash()` (a plain function, not a bus
subscriber — a crash has no event of its own, same reasoning `_record_task_outcome`
below already gives) for every job it finds `running` with nothing actually running
it, whatever verdict `classify_recovery()` reaches. That in turn calls
`capture.record_job_crash()`, which writes under its own `source: "job_crash"`
(never `"job"`) with a fresh, never-repeating `source_ref` — a crash is a new event
every time it happens, and must never collide with `record_job_outcome()`'s own
per-job dedup key, or a job that later finishes normally (or crashes twice) would
have one of those two facts silently dropped as a "duplicate" of the other.
`entity_ref` stays the job's own id either way, so both rows still group under the
same job. Closed; previously the process moved a crashed job to `queued`
(resumable/restartable) or `awaiting_decision` (unrecoverable) — none of those are
in `TERMINAL_JOB_STATUSES` — and published no event, so nothing about the crash
itself ever reached Self-Improvement.

**A split completion must never become an outcome.**
`jarvis/tools/job_split.py` marks the ORIGINAL job `status:'done'` with
`result: "Split into N separate job(s): ..."` and emits it exactly like a real
completion — that's a division of work, not a finished piece of it. `recordJobOutcome()`
excludes it two ways: the result-string prefix, and (more robust, in case the wording
ever changes) whether the job actually has children (`listJobs({parentId: job.id})`).

**A scheduled task's outcome is captured on EVERY run, not narrowed the way the memory
checkpoint beside it is.** `scheduler/engine.py:runTaskNow()` calls `recordTaskOutcome(run,
task)` synchronously, right next to the existing `checkpointFromText()` call — but
unlike that checkpoint (scoped to `prompt`-type actions on success only, since it costs
a real model call), capture costs nothing, so every action type and every outcome
(`ok` or not) is real signal worth keeping.

**`source_ref` and `entity_ref` are two different columns on purpose.** `source_ref` is
the per-EVENT dedup key (one job's own id; one scheduled RUN's own id). `entity_ref` is
the per-RECURRING-THING stable key (a job's id again, since a job never recurs; a
scheduled TASK's own saved id — not any one run's id — for a task outcome). Without this
split, `reflect.py`/`synthesize.py` would have no stable key to group "this specific
task tends to fail this way" under, since every run of the same task gets a fresh
`source_ref`.

## Lessons vs. rules vs. proposals vs. changes — the state machine

An `improvement_outcomes` row is a fact ("this happened"). An `improvement_lessons` row
(`kind:'lesson'` from `reflect.py`, `kind:'pattern'` from `improvement/synthesize.py`, either
kind possible from `improvement/reflect.py`) is an OBSERVATION — it never changes
behaviour by itself, and can be created from even a single outcome. An
`improvement_proposals` row is a candidate CHANGE — `synthesize.py` only ever creates a
`kind:'rule'` proposal once at least two lessons backed by at least two DISTINCT
underlying outcomes agree (`MIN_SUPPORTING_OUTCOMES`); `improvement/synthesize.py` creates
`kind:'idea'` proposals directly (never `rule`/`setting`, so `decide()`'s kind floor
already guarantees these never auto-apply); `tools/improvement_tools.py` (a conversational
tool) creates any kind but with `evidence: []`, which the evidence floor alone already
routes to always-ask. An `improvement_rules` row is the only thing ever actually
injected into the system prompt — `apply.py` is the only thing that ever creates one.
An `improvement_changes` row is the append-only audit/undo log — one row per applied
change, one MORE row (`kind:'undo'`) per undo, never a rewrite of the original.

**`synthesize.py` computes a rule proposal's `sourceTier` from the WORST (highest-
numbered) tier among its supporting lessons — never hardcoded to 1.** A pattern that
leans on even one lesson from `improvement/reflect.py` (tier 2-4) must never look tier-1 to
`improvement/policy.py`'s `decide()`, or an outside-sourced idea could slip past the
"outside ideas always ask" floor by riding along with genuinely tier-1 evidence. Found
and fixed during this build, before it ever shipped — worth remembering if this file is
ever refactored, since the natural-looking `sourceTier: 1` literal is exactly the kind
of thing that looks harmless in isolation.

## `improvement/policy.py`'s `decide()` — the one gate

Hard floors, in this order, none overridable by trust level: `kind` must be `'rule'` or
`'setting'` (a Skill/code/idea/conflict always asks); `sourceTier` must be exactly `1`
(only Jarvis's own directly-observed history, never anything read); `conflictWith` must
be unset (a contradiction always needs a human). Only past all three floors does trust
level matter at all — `MIN_EVIDENCE_BY_TRUST` (`ask: Infinity, balanced: 2, auto: 1`) is
a FLOOR beneath, not instead of, `synthesize.py`'s own code-level `MIN_SUPPORTING_OUTCOMES`
check — the two are independent belts-and-suspenders, not the same check twice.

## The budget ledger — a real spend cap, not a hoped-for cadence

`improvement/store.py`'s `tryConsumeDailyBudget()`/`tryConsumeWeeklyBudget()` (backed by
`db.py`'s existing `app_state` key/value table, the same one `chat_store.py`'s active
conversation id lives in — no new table needed) are the actual ceiling:
`DAILY_BUDGET = 2` shared between `reflect.py` and `synthesize.py`, `WEEKLY_BUDGET = 4`
shared between `improvement/reflect.py` and `improvement/synthesize.py`. `improvement/reflect.py` ticks every 15
minutes (96 times/day); without the budget AND each module's own cadence floor
(`reflect.py`'s 4-hour floor, `synthesize.py`'s 24-hour floor, both weekly modules'
7-day floor), a busy day's outcome backlog could otherwise trigger a call on nearly
every tick. Confirmed live (`phase4-verify.mjs`, this build's own verification script):
10 compressed ticks in a row produced exactly 2 model calls, not 10.

## `domains.py` — the life-pattern guard, biased the OPPOSITE way from the Node build's style floors (not ported — see the root CLAUDE.md)

the Node build's style floors' distress/serious-topic floors stay narrow on purpose — a false
positive there only costs tone. `domains.py`'s `isExcludedDomain()` is biased broad on
purpose — a false NEGATIVE here means relationship or emotional content reaches a model
call the user explicitly said should never happen, which is the actual harm this module
exists to prevent; a false positive just drops one memory/message from consideration,
costing nothing structurally. Applied on BOTH sides in `improvement/synthesize.py`: matching
memories/messages are filtered out of the input block before the model ever sees them
(`gatherInputs()`), and any produced insight that still matches is dropped after
(`isExcludedDomain(text)` in the response loop) — never trusted to a single side, and
never left to a prompt instruction alone (that's the backstop, not the mechanism).
Verified against the exact false-positive class the Node build's style floors' own regexes needed
fixing for: "I have a good relationship with this codebase" does not match, because
every relationship pattern is anchored to an actual personal-relationship noun, never a
bare "relationship" match.

## Undo — refuse, never clobber

`apply.py`'s `undoChange()` compares the LIVE value against what the change actually
set (`after`, not just `before` — `memory/store.py`'s `updateMemory()` only ever needs
the pre-edit state because Memory has no separate "did the user touch this since"
question; Self-Improvement does). If they differ — the user muted the rule, or edited
the pref, since Jarvis applied it — undo refuses with `{ok:false, reason:
'changed_since', current, expected}` rather than silently overwriting their own later
decision; `force:true` proceeds anyway, only after the caller has shown the user the
mismatch. An `undo` row is never itself undoable (bringing something back means
approving a fresh proposal, not reversing a reversal) — keeps the log's `before`/`after`
pair honest at every row, with no chain of reversals to reason through. Verified live,
browser-tested: externally muting an applied rule, then clicking Undo on its Change row,
produces the real confirm dialog, not a silent clobber.

## The screen (`frontend/components/screens/ImprovementScreen.tsx`, `#/improvement`)

Four tabs (`segmented()`, no hash sub-routing): **Suggestions** (pending proposals — a
"Show rejected" toggle reveals what was rejected, each with Restore/Delete permanently;
`skill`/`code` kinds get "Generate a prompt for a coding assistant" instead of a plain
Approve button, since generating that brief IS the approval action for those two kinds),
**Changes** (the undo log, human-readable titles resolved from a change's own `after`
snapshot rather than showing a raw rule id — including for a rule since deleted, which
still displays correctly since the title comes from the change's own stored snapshot,
never a live lookup), **Learned** (live rules with a mute toggle plus a "Show archived"
toggle, and recently-noticed lessons with their own "Show archived" toggle), **Settings**
(the trust dial, the outside-research toggle, and the two budget-remaining counters).
Same client-side-only filtering discipline as `routes/memory.py` — every list here is small and
curated by design.

**Every row is clickable, opening a detail view** (Phase 7 — the first thing the user's
own real test pass on this screen flagged as missing). A Rule's detail is the one place
text is actually editable (the user's own confirmed choice) — Save writes a real
`improvement_changes` row through the SAME undo machinery a freshly-applied rule already
uses, so an edit is undoable exactly like a creation, never a special case. Every detail
view also offers **Archive** (soft — hides the item behind its tab's "Show
archived"/"Show rejected" toggle) or, once already archived/rejected, **Restore** /
an armed **Delete permanently** — reusing Memory's own proven pattern rather than a new
"Recycle Bin" concept, on purpose (see the in-chat design discussion this session): a
Rule's own Undo button in Changes depends on the row still existing, so a genuine
hard-delete must always go through archive first, never be reachable directly from the
live list.

**A real bug, caught live during this exact `agent-browser` verification pass, worth
remembering if this file is touched again:** `frontend/components/ui/Modal.tsx`'s `openModal({build(body, api)})`
hands `build` an `api` object (with `api.close(result)`) specifically so a custom action
button INSIDE the modal body can close the dialog itself — the modal's own footer
buttons (Cancel/Save/the primary submit) already do this automatically, but a button
`build()` adds by hand does not, unless it calls `api.close()` itself. The first version
of every detail-view Archive/Restore/Delete/Approve/Reject button here called `onChange()`
but never `api.close()`, so the underlying list correctly refreshed while the modal (and
its scrim) stayed open on top of it, indistinguishable from the page being frozen. Every
terminal action in this file now explicitly closes its own modal after the action
completes — the general lesson: any custom button added inside a `build()` callback that
should end the dialog needs `build(body, api)`, not just `build(body)`, and must call
`api.close(...)` itself.

## Tools (`jarvis/tools/`)

**A tool's own `description` field is not, by itself, enough to make the model actually
call it — a real, live-confirmed gap in the first version of this subsystem.** Every one
of these four tools was declared correctly from day one, but `prompt.py`'s
`SYSTEM_INSTRUCTION` never had a paragraph telling the model WHEN to use them, unlike
every other subsystem in this app (Memory's own section is the pattern this was fixed to
match — see `prompt.py`, right after the Memory section). Without it, asking Jarvis to
teach it something or suggest an improvement produced a normal, reasonable-sounding
reply that filed nothing at all — confirmed live by the user's own first real test.
Fixed with an explicit trigger paragraph per tool; verified with a real stub model that
captures whether a `tool_calls` array was actually sent, not just by re-reading the
prompt text. Any FUTURE tool added to this subsystem needs the same explicit trigger
wording in `SYSTEM_INSTRUCTION`, not just a good `description` — the tool declaration
alone was never actually sufficient.

`tools/improvement_tools.py` (`core:true, meta:true` — the user just plainly taught Jarvis
something; files an `'explicit'`-source outcome via `capture.py`'s
`recordExplicitTeaching()` first, so the lesson it creates has a real outcome id to cite
as evidence like everything else, then goes through the SAME synthesis gate as any other
lesson — no bypass, even for a direct user statement). `tools/improvement_tools.py`
(`core:true, meta:true` — "what have you learned/changed"). `tools/improvement_tools.py`
(non-core — the model's own in-the-moment noticing, always `evidence: []`, so `decide()`
always asks). `tools/improvement_tools.py` (non-core, no confirm gate — undo is the safe
direction; surfaces a refused undo plainly rather than forcing it).
