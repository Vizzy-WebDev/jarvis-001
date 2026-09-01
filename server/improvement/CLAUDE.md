# Self-Improvement (`server/improvement/*.js`)

See the root `CLAUDE.md`'s "Self-Improvement" section for the decisions that matter
beyond this file (the lessons-vs-rules split, the undo refuse-vs-clobber design, the
budget ledger, the domain-exclusion guard). This file is the module-by-module
breakdown.

## The chain, and why it's five modules instead of one

`capture.js` (0 model calls) writes `improvement_outcomes` rows the instant a job/task
finishes or a plainly-worded correction fires. `reflect.js` (1 batched call) turns a
backlog of outcomes into individual `improvement_lessons` — observations, not yet
behaviour changes. `synthesize.js` (1 rarer call) looks ACROSS the whole active lesson
set for something that genuinely recurred and turns THAT into an `improvement_proposals`
row — a `rule`/`setting` proposal only ever gets created here when at least two
DISTINCT underlying outcomes support it (`MIN_SUPPORTING_OUTCOMES`), never from one
lesson however clearly phrased. `improvement-policy.js`'s `decide()` is the one seam
every path (auto-apply, the screen's Approve button, a conversational
`suggest_improvement` call) asks before anything actually changes; `apply.js` is the
only thing that ever writes a real rule or flips a real pref, and the only thing that
ever undoes one.

**`reflect.js` only marks a batch of outcomes reviewed when the model call actually
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
reflect/synthesize budget: `improve-research.js` (tiers 2-4 — official docs,
communities, general web, reusing `server/research.js`'s own free-web-then-model-search
path rather than reimplementing it) and `life-patterns.js` (patterns in the user's OWN
work/projects/study/finances/travel, never emotional state or relationships). Both feed
the SAME lesson/proposal pipeline above — there is exactly one place a lesson becomes a
proposal (`synthesize.js`), never a second path for outside-sourced material.

`implementation-prompt.js` is a sixth, on-demand-only module (not part of the
background cycle at all) — turns an approved `skill`/`code` proposal into a brief for
whichever coding assistant the user names, never reading the repo itself.

## Import discipline — why the split is exactly this shape

| Module | Imports | Why |
|---|---|---|
| `improvement-store.js` | **leaf** — `db.js` only | Safe for `server/tools/*.js` and `prompt.js` to import directly, same reasoning as `memory/memory-store.js` and `jobs/job-store.js` |
| `improvement-policy.js` | **pure** — `prefs.js` only | Testable with a bare `node -e` truth table, same as `memory/memory-policy.js` |
| `domains.js` | **zero imports** | Same reasoning as `personality.js` — a floor, directly testable, no server needed |
| `capture.js` | `improvement-store.js`, `jobs/job-store.js`, both leaves | Zero model calls, ever — safe to call from `models/runner.js`'s own per-turn loop and `scheduler.js`'s own outcome hook with no latency risk |
| `apply.js` | `improvement-store.js`, `prefs.js` | Never imports anything that could write to the repo's own source tree — see its own header comment on why that's the one invariant this whole module exists to protect |
| `reflect.js`, `synthesize.js`, `improve-research.js`, `life-patterns.js`, `implementation-prompt.js` | + `ai.js` (and, for `improve-research.js`, `research.js`) | Not leaf — safe to import from `cycle.js` only |
| `cycle.js` | everything above | The only non-leaf top-level module; `startImprovementCycle()` called once from `server.js`, beside `startScheduler()`/`startOrchestrator()` |

`capture.js` being callable from `models/runner.js` (for `noteCorrection()`, gated on
`!opts.background`) and from `scheduler.js` (for `recordTaskOutcome()`, beside the
existing memory checkpoint hook) is what makes capture cost nothing structurally — a
leaf module three hops away from any model call can't accidentally spend quota no
matter where it's called from.

## The hook points — verified, not assumed

A job's terminal status is caught two ways: `cycle.js` subscribes directly to
`jobs/job-events.js`'s `jobEvents` (a pure leaf — deliberately NOT
`jobs/orchestrator.js`, which would be the non-leaf-but-still-safe-here choice but is
reserved for `server.js` alone per that directory's own circular-import note) for
`'status'` events with `done`/`failed`/`cancelled`; a crash-classified `orphaned` job
never emits at all (`orchestrator.js`'s `recoverOrphans()` writes the row directly), so
`cycle.js`'s own tick sweeps for it separately via `jobStore.listJobs({status:
'orphaned'})`. Both paths funnel into `capture.js`'s `recordJobOutcome(jobId)`, which is
idempotent by construction (`improvement_outcomes.UNIQUE(source, source_ref)` +
`INSERT OR IGNORE`) — confirmed live that `orchestrator.js`/`worker.js` really can emit
`'status'` twice for the same job, and a repeat sweep of an already-captured orphan
really is a no-op, not a growing cost.

**A split completion must never become an outcome.**
`server/tools/request_job_split.js` marks the ORIGINAL job `status:'done'` with
`result: "Split into N separate job(s): ..."` and emits it exactly like a real
completion — that's a division of work, not a finished piece of it. `recordJobOutcome()`
excludes it two ways: the result-string prefix, and (more robust, in case the wording
ever changes) whether the job actually has children (`listJobs({parentId: job.id})`).

**A scheduled task's outcome is captured on EVERY run, not narrowed the way the memory
checkpoint beside it is.** `scheduler.js:runTaskNow()` calls `recordTaskOutcome(run,
task)` synchronously, right next to the existing `checkpointFromText()` call — but
unlike that checkpoint (scoped to `prompt`-type actions on success only, since it costs
a real model call), capture costs nothing, so every action type and every outcome
(`ok` or not) is real signal worth keeping.

**`source_ref` and `entity_ref` are two different columns on purpose.** `source_ref` is
the per-EVENT dedup key (one job's own id; one scheduled RUN's own id). `entity_ref` is
the per-RECURRING-THING stable key (a job's id again, since a job never recurs; a
scheduled TASK's own saved id — not any one run's id — for a task outcome). Without this
split, `reflect.js`/`synthesize.js` would have no stable key to group "this specific
task tends to fail this way" under, since every run of the same task gets a fresh
`source_ref`.

## Lessons vs. rules vs. proposals vs. changes — the state machine

An `improvement_outcomes` row is a fact ("this happened"). An `improvement_lessons` row
(`kind:'lesson'` from `reflect.js`, `kind:'pattern'` from `life-patterns.js`, either
kind possible from `improve-research.js`) is an OBSERVATION — it never changes
behaviour by itself, and can be created from even a single outcome. An
`improvement_proposals` row is a candidate CHANGE — `synthesize.js` only ever creates a
`kind:'rule'` proposal once at least two lessons backed by at least two DISTINCT
underlying outcomes agree (`MIN_SUPPORTING_OUTCOMES`); `life-patterns.js` creates
`kind:'idea'` proposals directly (never `rule`/`setting`, so `decide()`'s kind floor
already guarantees these never auto-apply); `suggest_improvement.js` (a conversational
tool) creates any kind but with `evidence: []`, which the evidence floor alone already
routes to always-ask. An `improvement_rules` row is the only thing ever actually
injected into the system prompt — `apply.js` is the only thing that ever creates one.
An `improvement_changes` row is the append-only audit/undo log — one row per applied
change, one MORE row (`kind:'undo'`) per undo, never a rewrite of the original.

**`synthesize.js` computes a rule proposal's `sourceTier` from the WORST (highest-
numbered) tier among its supporting lessons — never hardcoded to 1.** A pattern that
leans on even one lesson from `improve-research.js` (tier 2-4) must never look tier-1 to
`improvement-policy.js`'s `decide()`, or an outside-sourced idea could slip past the
"outside ideas always ask" floor by riding along with genuinely tier-1 evidence. Found
and fixed during this build, before it ever shipped — worth remembering if this file is
ever refactored, since the natural-looking `sourceTier: 1` literal is exactly the kind
of thing that looks harmless in isolation.

## `improvement-policy.js`'s `decide()` — the one gate

Hard floors, in this order, none overridable by trust level: `kind` must be `'rule'` or
`'setting'` (a Skill/code/idea/conflict always asks); `sourceTier` must be exactly `1`
(only Jarvis's own directly-observed history, never anything read); `conflictWith` must
be unset (a contradiction always needs a human). Only past all three floors does trust
level matter at all — `MIN_EVIDENCE_BY_TRUST` (`ask: Infinity, balanced: 2, auto: 1`) is
a FLOOR beneath, not instead of, `synthesize.js`'s own code-level `MIN_SUPPORTING_OUTCOMES`
check — the two are independent belts-and-suspenders, not the same check twice.

## The budget ledger — a real spend cap, not a hoped-for cadence

`improvement-store.js`'s `tryConsumeDailyBudget()`/`tryConsumeWeeklyBudget()` (backed by
`db.js`'s existing `app_state` key/value table, the same one `chat-store.js`'s active
conversation id lives in — no new table needed) are the actual ceiling:
`DAILY_BUDGET = 2` shared between `reflect.js` and `synthesize.js`, `WEEKLY_BUDGET = 4`
shared between `improve-research.js` and `life-patterns.js`. `cycle.js` ticks every 15
minutes (96 times/day); without the budget AND each module's own cadence floor
(`reflect.js`'s 4-hour floor, `synthesize.js`'s 24-hour floor, both weekly modules'
7-day floor), a busy day's outcome backlog could otherwise trigger a call on nearly
every tick. Confirmed live (`phase4-verify.mjs`, this build's own verification script):
10 compressed ticks in a row produced exactly 2 model calls, not 10.

## `domains.js` — the life-pattern guard, biased the OPPOSITE way from `personality.js`

`personality.js`'s distress/serious-topic floors stay narrow on purpose — a false
positive there only costs tone. `domains.js`'s `isExcludedDomain()` is biased broad on
purpose — a false NEGATIVE here means relationship or emotional content reaches a model
call the user explicitly said should never happen, which is the actual harm this module
exists to prevent; a false positive just drops one memory/message from consideration,
costing nothing structurally. Applied on BOTH sides in `life-patterns.js`: matching
memories/messages are filtered out of the input block before the model ever sees them
(`gatherInputs()`), and any produced insight that still matches is dropped after
(`isExcludedDomain(text)` in the response loop) — never trusted to a single side, and
never left to a prompt instruction alone (that's the backstop, not the mechanism).
Verified against the exact false-positive class `personality.js`'s own regexes needed
fixing for: "I have a good relationship with this codebase" does not match, because
every relationship pattern is anchored to an actual personal-relationship noun, never a
bare "relationship" match.

## Undo — refuse, never clobber

`apply.js`'s `undoChange()` compares the LIVE value against what the change actually
set (`after`, not just `before` — `memory-store.js`'s `updateMemory()` only ever needs
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

## The screen (`public/screens/improvement.js`, `#/improvement`)

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
Same client-side-only filtering discipline as `memory.js` — every list here is small and
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
remembering if this file is touched again:** `_modal.js`'s `openModal({build(body, api)})`
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

## Tools (`server/tools/`)

**A tool's own `description` field is not, by itself, enough to make the model actually
call it — a real, live-confirmed gap in the first version of this subsystem.** Every one
of these four tools was declared correctly from day one, but `prompt.js`'s
`SYSTEM_INSTRUCTION` never had a paragraph telling the model WHEN to use them, unlike
every other subsystem in this app (Memory's own section is the pattern this was fixed to
match — see `prompt.js`, right after the Memory section). Without it, asking Jarvis to
teach it something or suggest an improvement produced a normal, reasonable-sounding
reply that filed nothing at all — confirmed live by the user's own first real test.
Fixed with an explicit trigger paragraph per tool; verified with a real stub model that
captures whether a `tool_calls` array was actually sent, not just by re-reading the
prompt text. Any FUTURE tool added to this subsystem needs the same explicit trigger
wording in `SYSTEM_INSTRUCTION`, not just a good `description` — the tool declaration
alone was never actually sufficient.

`record_lesson.js` (`core:true, meta:true` — the user just plainly taught Jarvis
something; files an `'explicit'`-source outcome via `capture.js`'s
`recordExplicitTeaching()` first, so the lesson it creates has a real outcome id to cite
as evidence like everything else, then goes through the SAME synthesis gate as any other
lesson — no bypass, even for a direct user statement). `review_improvements.js`
(`core:true, meta:true` — "what have you learned/changed"). `suggest_improvement.js`
(non-core — the model's own in-the-moment noticing, always `evidence: []`, so `decide()`
always asks). `undo_improvement.js` (non-core, no confirm gate — undo is the safe
direction; surfaces a refused undo plainly rather than forcing it).
