# Self-Model (`server/self/*.js`)

See the root `CLAUDE.md`'s "Self-Model" section for the decisions that matter beyond this
file (the grounding rule, the authority ceiling, the hybrid trigger design, why it reads
Self-Improvement instead of duplicating it). This file is the module-by-module breakdown.

## The invariant every module here exists to protect

`self-model.js` produces **read-only text and data**, exactly the way `personality.js`
produces prose appended to a delivery instruction and nothing else. Enforced by the
import graph, not by convention: nothing under `server/self/` imports
`capabilities.js`, `tools/index.js`, `models/runner.js`, `scheduler/*`, or
`control/session.js` — verified live (`node -e "import('./server/tools/index.js')"`
loads cleanly with `check_myself`/`track_goal` both present, and a grep of every
`server/self/*.js` file for those five module paths turns up only doc-comment mentions,
never a real `import`). A self-assessment computed here can inform what Jarvis *says*;
it structurally cannot reach `memory-policy.js`'s `decide()`, `improvement-policy.js`'s
`decide()`, the confirm gate, or `job-actions.js` — there is no import edge for it to
travel through even if a future edit tried.

**Zero model calls, anywhere in this directory.** Every read is a cheap SQLite query or
an in-memory registry lookup — grep confirms no file here imports `ai.js`,
`models/registry.js`'s `getAdapter`, or calls `adapter.stream`/`askModel`. This matters
given how often every model in this project is rate-limited (see the "Free-tier quota"
Gotcha in root `CLAUDE.md`) — the self-model has to work even when nothing else can
answer.

## Modules

- **`self-store.js`** — leaf (imports only `db.js`). The two new tables from `db.js`
  migration 9: `self_capability_stats` (a rolling per-`(axis, key)` tally —
  `recordAttempt(axis, key, ok)` bumps it, `getStat`/`listStats` read it; a row that has
  never been written returns `null`, never a zeroed-out fake) and `self_goals` (one
  active row per `(scope_kind, scope_ref)` — `declareGoal()` closes any prior active goal
  for the same scope before inserting a new one, so there's never more than one active
  goal per scope at a time; `closeGoal()`/`getActiveGoal()` round it out). Deliberately
  holds **no prose knowledge and no facts about the user** — that's what keeps this from
  being the "second memory-like store" the build was explicitly told never to create;
  everything content-shaped keeps going through `improvement/improvement-store.js`'s
  existing tables instead.
- **`self-signals.js`** — zero imports, pure. `detectSelfSignals()` takes only plain data
  a caller has already computed from real state (never touches a store itself) and
  returns which of five triggers fired: `authority`, `knownFailure`, `noTrackRecord`,
  `correction`, `blockedOnBackground`. `anySignalFired()` is the one gate `prompt.js`'s
  `selfFocusSection()` checks before spending any tokens on it. Testable with a bare
  `node --input-type=module -e "..."` script — no server, no database — same as
  `personality.js`'s `detectFloors()`.
- **`self-capture.js`** — leaf-adjacent (imports `self-store.js` and
  `improvement/improvement-store.js`, both leaves). `recordToolOutcome()` is called once
  per `tool_result` from `models/runner.js`'s own per-step loop: **every** outcome bumps
  the rolling tally (dimension 2's whole grounding), but only a **notable** one (a real
  failure, a refused-allowlist call, or an escalated confirm) also writes one more
  `improvement_outcomes` row (`source: 'turn'`) into Self-Improvement's *existing*
  pipeline — this file is the one place this build feeds that pipeline, and it feeds the
  one door it already has, never a second one.
- **`self-model.js`** — the assembler. Not a leaf (imports several stores), but every one
  of those is itself leaf or leaf-adjacent, which is what keeps this file safe for
  `server/tools/check_myself.js` to import directly. `buildSelfModel({ only, ... })`
  dispatches to one builder function per dimension (see `DIMENSION_KEYS`) — omitting
  `only` builds nothing at all, on purpose, never a default "everything." Every builder
  returns a `verdict`/`grounded` field alongside its data; below `MIN_ATTEMPTS_FOR_RATIO`
  (5) a tally is reported as `no_track_record`, never a premature ratio.
  `computeTurnSignals()` is the live-data half of `detectSelfSignals()` — reads the real
  current state (active jobs, pending memory conflicts, active lesson/rule scopes, tool
  stats) and hands it to the pure function, keeping `self-signals.js` itself zero-import.
- **`CLAUDE.md`** — this file.

## Dimension grounding, one line each (see `self-model.js`'s own comments for the full detail)

1. **What it is** — live counts (models, connectors, Skills, active jobs) plus a short,
   hand-written structural description explicitly marked `verified: false` — the weakest
   grounding in the build, and it says so rather than passing as fact.
2. **What it can/can't do** — `self_capability_stats` for tools; `improvement-store.js`'s
   new `outcomeReliability()` aggregate for job kinds/task types (no rolling tally exists
   for those two axes in this build — see `self-store.js`'s header comment on why).
3. **How it behaves** — a read-only VIEW over `improvement-store.js`'s `listRules()` —
   owns none of this data.
4. **Doing now, and why** — active jobs, the goal declared for this session
   (`self_goals`), and Personality's *own already-computed* `readStyle()` result,
   reported, never re-decided (see the header invariant).
5. **How it knows** — real memory `origin`/date on a text match, real provenance-kind
   descriptions, otherwise labelled general knowledge — never invents a category.
6. **Its call to make** — reads the *actual live values* out of `memory-policy.js`'s
   `THRESHOLDS` and `improvement-policy.js`'s `MIN_EVIDENCE_BY_TRUST`/hard floors, so this
   can never drift from what those modules really enforce.
7. **How it fails** — scoped `improvement_lessons`, filtered to what's actually relevant.
8. **Works with the user** — corrections/explicit-teaching outcome counts; reported as
   genuinely thin below the same 5-attempt floor as everything else.
9. **Goal, on track** — `self_goals` for a live conversation (a job already has its own
   durable `goal` column — see `jobs/job-store.js`). Always reported as *what Jarvis
   recorded it understood*, never as a verified account of what the user meant.

## The trigger design — push vs. pull, and the real limitation of push

`self-signals.js`'s five triggers are computed fresh every step of
`models/runner.js`'s own tool-calling loop (`computeTurnSignals()`, not once per turn) —
`authority`/`blockedOnBackground` read state that's knowable before the turn even starts
(pending job/memory decisions); `correction` reuses `improvement/capture.js`'s
`noteCorrection()` return value from the same turn rather than a second regex pass.
**`knownFailure`/`noTrackRecord` can only ever match a tool THIS turn has already called
in an earlier step** (`usedToolNames`, accumulated across the loop) — there's no way to
warn about a tool before the model decides to call it for the first time in a turn, since
nothing here has foreknowledge of that decision. This is an accepted, structural
limitation of a push-only mechanism, not a bug — the **pull** path
(`check_myself`'s `can_do`/`failure_modes` dimensions) is what catches it proactively,
*before* any use at all, exactly when the model chooses to check first. `prompt.js`'s
system-instruction paragraph tells it to.

## `prompt.js` integration

`selfSection()` (stable, cacheable, no DB read) carries the one hard governing rule;
`selfFocusSection(signals)` (volatile, emitted only when something actually fired) is the
push half. Both are deliberately **ungated by `background`**, same reasoning
`improvementSection()` already uses — a background Job's own turn benefits from knowing
it's blocked on something or heading into a known failure just as much as live chat does.
`correction` simply never fires on a background turn, since `noteCorrection()` itself is
gated on `!opts.background`.

## Tools (`server/tools/`)

`check_myself.js` (`core:true, meta:true` — the pull path; no reliable search-intent text
to find it by otherwise) and `track_goal.js` (`core:true, meta:true` — records what
Jarvis understands the current goal to be; no confirm gate, since recording an
understanding has no outward effect of its own to protect).

## `capabilities.js`'s ctx injection

`invoke()`'s existing pattern (`reservedSkillNames`, `searchCapabilities`, `invoke`
itself, injected into every capability's `ctx` — see that file's own header comment) now
also injects `listCapabilities` — the one piece of dimension 1's live counts
(`self-model.js`'s `whatItIs()`) that can only come from `capabilities.js`, which this
directory can never import directly.
