<!-- Ported from the Node build during the S6 cutover. The architecture, the
invariants and the live-caught bugs described here all carried over deliberately and
still hold. File paths have been updated to their real Python counterparts and are
verified to exist. Function names written in camelCase (`getToolDeclarations()`) are
the NODE originals, kept because the surrounding reasoning is about them; the Python
equivalent is the snake_case function doing that job in the same module. Where a Node
module had no Python counterpart, the text says so rather than pointing at a file that
does not exist. -->

# Self-Model (`jarvis/self/*.py`)

See the root `CLAUDE.md`'s "Self-Model" section for the decisions that matter beyond this
file (the grounding rule, the authority ceiling, the hybrid trigger design, why it reads
Self-Improvement instead of duplicating it). This file is the module-by-module breakdown.

## The invariant every module here exists to protect

`self/model.py` produces **read-only text and data**, exactly the way the Node build's style floors (not ported — see the root CLAUDE.md)
produces prose appended to a delivery instruction and nothing else. Enforced by the
import graph, not by convention: nothing under `jarvis/self/` imports
`capabilities/`, `capabilities/registry.py`, `orchestrator/pipeline.py`, `scheduler/*`, or
`control/session.py` — verified live (`python -c "import jarvis.capabilities.registry"`
loads cleanly with `check_myself`/`track_goal` both present, and a grep of every
`jarvis/self/*.py` file for those five module paths turns up only doc-comment mentions,
never a real `import`). A self-assessment computed here can inform what Jarvis *says*;
it structurally cannot reach `memory/policy.py`'s `decide()`, `improvement/policy.py`'s
`decide()`, the confirm gate, or `orchestrator.py` — there is no import edge for it to
travel through even if a future edit tried.

**Zero model calls, anywhere in this directory.** Every read is a cheap SQLite query or
an in-memory registry lookup — grep confirms no file here imports `ai.py`,
`gateway/registry.py`'s `getAdapter`, or calls `adapter.stream`/`askModel`. This matters
given how often every model in this project is rate-limited (see the "Free-tier quota"
Gotcha in root `CLAUDE.md`) — the self-model has to work even when nothing else can
answer.

## Modules

- **`self/store.py`** — leaf (imports only `db.py`). Two tables from `db.py` migration
  9: `self_capability_stats` (a rolling per-`(axis, key)` tally —
  `recordAttempt(axis, key, ok)` bumps it, `getStat`/`listStats` read it; a row that has
  never been written returns `null`, never a zeroed-out fake) and `self_goals` (one
  active row per `(scope_kind, scope_ref)` — `declareGoal()` closes any prior active goal
  for the same scope before inserting a new one, so there's never more than one active
  goal per scope at a time; `closeGoal()`/`getActiveGoal()` round it out). Plus one more
  from migration 11: `capture_health` (`recordCaptureHealth()`/`captureHealthSummary()`)
  — the health of the CAPTURE MECHANISM itself, a different axis from what
  `self_capability_stats` measures; see the capture-wiring entry below. Two more
  from migration 12: `self_model_snapshots` (`saveSelfModelSnapshot()`/
  `getSelfModelSnapshot()`/`getSnapshotByToolCallId()` — the exact JSON a `check_myself`
  call returned, kept permanently) and `self_model_citations`
  (`recordSelfModelCitation()`/`listCitationsForSnapshot()` — one row per numeric,
  checkable fact a snapshot contained, logged as a CANDIDATE the instant the snapshot is
  taken, never as a confirmed verdict); see `self/verify.py`'s own entry below for the
  actual check built on top of these two. All five deliberately hold **no prose
  knowledge and no facts about the user** — that's what keeps this from being the
  "second memory-like store" the build was explicitly told never to create; everything
  content-shaped keeps going through `improvement/improvement/store.py`'s existing
  tables instead.
- **`self/signals.py`** — zero imports, pure. `detectSelfSignals()` takes only plain data
  a caller has already computed from real state (never touches a store itself) and
  returns which of five triggers fired: `authority`, `knownFailure`, `noTrackRecord`,
  `correction`, `blockedOnBackground`. `anySignalFired()` is the one gate `prompt.py`'s
  `selfFocusSection()` checks before spending any tokens on it. Testable with a bare
  `python -c "..."` script — no server, no database — same as
  the Node build's style floors' `detectFloors()`.
- **No separate `self-capture.py` file exists in this port — worth being explicit,
  since the Node original had one and this doc used to describe it as if it still
  did.** Its rolling-tally half survives, folded directly into `self/store.py`'s own
  `record_attempt(axis, key, ok)` — called from `observers/recording.py`'s
  `_record_tool_outcome`, subscribed to `EventType.TOOL_COMPLETED`/`TOOL_FAILED` and
  wired up unconditionally at real startup via `start_observers()`, never by a direct
  call from the turn loop (`orchestrator/pipeline.py` deliberately never imports
  `jarvis.self`, the same invariant that keeps it clear of `jarvis.improvement`,
  `jarvis.cost`, `jarvis.observers`; `test_architecture.py` asserts it). **What did
  NOT survive the port: the other half.** The Node original's `recordToolOutcome()`
  also wrote a fresh `improvement_outcomes` row (`source: 'turn'`) for a *notable*
  outcome (a real failure, a refused-allowlist call, an escalated confirm), feeding
  Self-Improvement's pipeline from live tool activity in the same turn it happened.
  Nothing in this Python port does that — a tool's failure bumps the Self-Model tally
  and nothing else; Self-Improvement's own capture (`observers/improvement.py`) only
  ever hears about a job's or a scheduled task's terminal status, never a single
  notable tool call inside an ordinary live turn. A real, disclosed gap, not yet
  built. **`record_attempt()`'s own error handling did survive the port intact**:
  wrapped in its own local `try/except`, and either branch logs to `self/store.py`'s
  `capture_health` table (`record_capture_health()`) — the health of the SENSOR,
  distinct from what it measures, still read by `ops/diagnostics/checks/capture_health.py`.
- **`self/model.py`** — the assembler. Not a leaf (imports several stores), but every one
  of those is itself leaf or leaf-adjacent, which is what keeps this file safe for
  `jarvis/tools/self_tools.py` to import directly. `buildSelfModel({ only, ... })`
  dispatches to one builder function per dimension (see `DIMENSION_KEYS`) — omitting
  `only` builds nothing at all, on purpose, never a default "everything." Every builder
  returns a `verdict`/`grounded` field alongside its data; below `MIN_ATTEMPTS_FOR_RATIO`
  (5) a tally is reported as `no_track_record`, never a premature ratio.
  `computeTurnSignals()` is the live-data half of `detectSelfSignals()` — reads the real
  current state (active jobs, pending memory conflicts, active lesson/rule scopes, tool
  stats) and hands it to the pure function, keeping `self/signals.py` itself zero-import.
  Also exports `extractCitableFields(snapshot)` and `getByPath(obj, path)` — the
  mechanical, key-name-blocklist walker `tools/self_tools.py` uses to find every NUMERIC
  leaf in its own returned snapshot (see `self/verify.py` below for why only numbers).
- **`self/verify.py`** — leaf-adjacent (imports `self/store.py` and `../chat_store.py`,
  both leaves; `self/store.py` itself deliberately never imports `chat_store.py`, so
  this is the one place the two meet). The actual utterance-provenance check:
  `verifyCitation(snapshotId, toolCallId, fieldName)` re-reads the real snapshot AND the
  real reply that followed it — via `chat_store.py`'s own `getMessages()`, correlating
  by the tool call's own persisted id, never a new schema field on `messages` — and
  returns `used` / `ignored` / `unverifiable`. Never trusts `self_model_citations`' own
  stored `field_value`; always re-derives it fresh from the snapshot, so a stale
  citation row can never produce a wrong verdict. Deliberately does NOT attempt to
  verify free-form prose (not solvable) — a non-numeric field always returns
  `unverifiable`, the honest outcome, never guessed at either way. Purely forensic today
  — no tool exposes it to a model; it's meant for a one-off script the way earlier real
  bugs in this project were diagnosed (root CLAUDE.md's "No automated test suite"
  section).
- **`CLAUDE.md`** — this file.

## Dimension grounding, one line each (see `self/model.py`'s own comments for the full detail)

1. **What it is** — live counts (models, connectors, Skills, active jobs) plus a short,
   hand-written structural description explicitly marked `verified: false` — the weakest
   grounding in the build, and it says so rather than passing as fact.
2. **What it can/can't do** — `self_capability_stats` for tools; `improvement/store.py`'s
   new `outcomeReliability()` aggregate for job kinds/task types (no rolling tally exists
   for those two axes in this build — see `self/store.py`'s header comment on why).
   `sensorHealth` (`self/store.py`'s `captureHealthSummary()`, always included in this
   dimension's response) reports the health of the RECORDER itself — a
   `no_track_record` verdict elsewhere in the same response can now be told apart from
   "never used" versus "the thing that would have noticed it was used is broken."
3. **How it behaves** — a read-only VIEW over `improvement/store.py`'s `listRules()` —
   owns none of this data.
4. **Doing now, and why** — active jobs, the goal declared for this session
   (`self_goals`), and Personality's *own already-computed* `readStyle()` result,
   reported, never re-decided (see the header invariant).
5. **How it knows** — real memory `origin`/date on a text match, real provenance-kind
   descriptions, otherwise labelled general knowledge — never invents a category. Every
   real `check_myself` call is also persisted (`self_model_snapshots`) with its
   checkable NUMERIC facts logged as citation candidates — `self/verify.py`'s
   `verifyCitation()` is the actual, independent check of whether a given call's own
   reply used one, closing the audit's central finding for the one narrow slice of
   "did the sentence match the data" that plain string matching can answer.
6. **Its call to make** — reads the *actual live values* out of `memory/policy.py`'s
   `THRESHOLDS` and `improvement/policy.py`'s `MIN_EVIDENCE_BY_TRUST`/hard floors, so this
   can never drift from what those modules really enforce. **Fix 4 (audit remediation):**
   the two numeric floors' own explanatory sentences (`memoryApprovalFloorText()`/
   `improvementEvidenceFloorText()`, `self/model.py`) are now built FROM the live number
   as their own template argument, not hand-typed nearby — the prose and the number it
   describes are the same read, so they can't independently drift apart. Regression-
   tested by mutating `THRESHOLDS`/`MIN_EVIDENCE_BY_TRUST` directly at runtime (both are
   plain exported `const` objects, not frozen — no module-mocking needed) and confirming
   the returned sentence picks up the new number. The three purely structural floors
   (kind, source tier, no conflict) are untouched — none of them reference a live
   number, so there was nothing for them to drift from.
7. **How it fails** — scoped `improvement_lessons`, filtered to what's actually relevant.
8. **Works with the user** — corrections/explicit-teaching outcome counts; reported as
   genuinely thin below the same 5-attempt floor as everything else.
9. **Goal, on track** — `self_goals` for a live conversation (a job already has its own
   durable `goal` column — see `jobs/job_store.py`). Always reported as *what Jarvis
   recorded it understood*, never as a verified account of what the user meant.
   **Fix 3 (audit remediation):** every declared goal now also snapshots the real text
   of the user's own most recent message at declare time (`tools/self_tools.py`'s
   `latestUserTurnText()`, reading `conversation.py`'s live window — never a second
   source of truth). Deliberately NOT a computed aligned/drifted/ambiguous verdict —
   judging whether a goal actually matches a real request is a semantic question no
   plain code can honestly answer, the same class of problem `self/verify.py` was
   built to stay clear of. The owner's own explicit choice: hand the model both real
   texts side by side and let it judge freshly each time it checks in, the same way
   dimension 6 hands it real policy numbers instead of a pre-baked answer.
   `sourceTurnText` is honestly `null` when nothing was captured (a goal declared
   before this column existed, or no user message existed yet) — the dimension's own
   `instruction` says so plainly rather than letting the model assume alignment it has
   no real basis for.

## The trigger design — push vs. pull, and the real limitation of push

`self/signals.py`'s five triggers are computed fresh every step of
`orchestrator/pipeline.py`'s own tool-calling loop (`computeTurnSignals()`, not once per turn) —
`authority`/`blockedOnBackground` read state that's knowable before the turn even starts
(pending job/memory decisions); `correction` reuses `improvement/capture.py`'s
`noteCorrection()` return value from the same turn rather than a second regex pass.
**`knownFailure`/`noTrackRecord` can only ever match a tool THIS turn has already called
in an earlier step** (`usedToolNames`, accumulated across the loop) — there's no way to
warn about a tool before the model decides to call it for the first time in a turn, since
nothing here has foreknowledge of that decision. This is an accepted, structural
limitation of a push-only mechanism, not a bug — the **pull** path
(`check_myself`'s `can_do`/`failure_modes` dimensions) is what catches it proactively,
*before* any use at all, exactly when the model chooses to check first. `prompt.py`'s
system-instruction paragraph tells it to.

## `prompt.py` integration

`selfSection()` (stable, cacheable, no DB read) carries the one hard governing rule;
`selfFocusSection(signals)` (volatile, emitted only when something actually fired) is the
push half. Both are deliberately **ungated by `background`**, same reasoning
`improvementSection()` already uses — a background Job's own turn benefits from knowing
it's blocked on something or heading into a known failure just as much as live chat does.
`correction` simply never fires on a background turn, since `noteCorrection()` itself is
gated on `!opts.background`.

## Tools (`jarvis/tools/`)

`tools/self_tools.py` (`core:true, meta:true` — the pull path; no reliable search-intent text
to find it by otherwise) and `tools/self_tools.py` (`core:true, meta:true` — records what
Jarvis understands the current goal to be; no confirm gate, since recording an
understanding has no outward effect of its own to protect). `tools/self_tools.py` is the one
tool file in this directory that imports `../conversation.py` directly (leaf-safe —
`conversation.py` itself only imports `chat_store.py`) — it reads the live conversation
window to snapshot the real user-turn text a goal is declared from (Fix 3, audit
remediation).

## `capabilities/`'s ctx injection

`invoke()`'s existing pattern (`reservedSkillNames`, `searchCapabilities`, `invoke`
itself, injected into every capability's `ctx` — see that file's own header comment) now
also injects `listCapabilities` — the one piece of dimension 1's live counts
(`self/model.py`'s `whatItIs()`) that can only come from `capabilities/`, which this
directory can never import directly.

A separate, one-hop-earlier addition: `orchestrator/pipeline.py` itself now passes
`toolCallId: call.id` into the `ctx` object it builds for every `invoke()` call (the
same object `turnId`/`style` already ride in) — this is runner.py's own ctx, not
something `capabilities/` injects, since it needs the real tool call's own id from
the adapter's own response, which only runner.py's per-step loop ever sees.
`tools/self_tools.py` uses this exact id to link its snapshot/citation rows back to the
real, persisted `messages` row `self/verify.py`'s `verifyCitation()` later reads.
