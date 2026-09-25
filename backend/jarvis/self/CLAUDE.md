# Self-Model (`jarvis/self/`)

See the root `CLAUDE.md`'s "Self-Model" section for the decisions that matter beyond this file
(the grounding rule, the authority ceiling, why it reads Self-Improvement instead of duplicating
it). This file is the module-by-module breakdown.

## The invariant every module here exists to protect

`self/model.py` produces **read-only text and data**. A self-assessment can make Jarvis sound
more or less confident about a claim; it structurally cannot skip a confirmation or an
approval. That is enforced by the import graph rather than by convention: nothing under
`jarvis/self/` imports the executor, the orchestrator or the capability
registry, so there is no edge for a self-assessment to travel through to `memory/policy.py`,
`improvement/policy.py` or the approval gate. `tests/test_architecture.py` asserts it.

**Zero model calls anywhere in this directory.** Every read is a cheap SQLite query or an
in-memory lookup. That matters because a model is often unavailable: the self-model has to
work even when nothing else can answer.

## Modules

- **`store.py`** — leaf (imports only `db.py`). It holds no prose knowledge and no facts about
  the user (that would be a second memory-like store); it holds only:
  - `self_capability_stats` — a rolling per-`(axis, key)` tally. `record_attempt(axis, key,
    ok)` bumps it; `get_stat()` / `list_stats()` read it. A row never written returns `None`,
    never a zeroed-out fake.
  - `self_goals` — at most one active goal per `(scope_kind, scope_ref)`. `declare_goal()`
    closes the prior active goal for that scope first; `get_active_goal()` / `close_goal()`.
    A goal also stores `source_turn_text`, the real text of the user's latest message at declare
    time (honestly `None` when none existed).
  - `capture_health` — `record_capture_health()` / `capture_health_summary()`: the health of
    the RECORDER itself. Without it, "never used" and "the thing that records usage is
    broken" are indistinguishable. Read by `ops/diagnostics/checks/capture_health.py`.
  - `self_model_snapshots` and `self_model_citations` — `save_snapshot()` keeps the exact JSON
    a `check_myself` call returned and logs each numeric, checkable fact in it as a citation
    CANDIDATE via `record_citation()`. Never a confirmed verdict.
- **`signals.py`** — zero imports, pure. `detect()` takes plain data a caller has already
  gathered and returns which of five triggers fired: `authority`, `knownFailure`,
  `noTrackRecord`, `correction`, `blockedOnBackground`. `any_fired()` is the gate before any
  prompt space is spent. It decides WHEN the self-model is worth consulting, never WHAT.
- **`model.py`** — the assembler. Not a leaf, but every store it imports is a leaf or
  leaf-adjacent. `build(only=[...])` dispatches to one builder per dimension in `BUILDERS`;
  omitting `only` builds NOTHING, on purpose. Below `MIN_ATTEMPTS_FOR_RATIO` (5) a reliability
  question returns `no_track_record`, never a premature ratio.
- **`verify.py`** — `verify_citation(snapshot_id, field_name, reply_text)` checks whether a
  reply actually used a number it was given, returning `used` / `ignored` / `unverifiable`. It
  re-derives the value from the stored snapshot rather than trusting the citation row, and it
  never tries to verify free-form prose (a non-numeric field is always `unverifiable`).
  `mentions_number()` is word-boundary safe on digits — substring matching made a citable `0`
  match inside `"100%"`. Purely forensic today: no tool exposes it to a model.

## Dimensions (`model.BUILDERS`), one line each

1. **`can_do`** — `self_capability_stats` for tools, and `improvement/store.py`'s outcome
   aggregate for job kinds and task types. A `recorder` entry (the capture-health summary) is
   included, so `no_track_record` can be told apart from "the recorder is broken".
2. **`failure_modes`** — scoped `improvement_lessons`, filtered to what is relevant.
3. **`how_it_behaves`** — a read-only view over Self-Improvement's active rules; owns none of
   that data.
4. **`doing_now`** — active jobs, the goal declared for this session, and the Adaptive
   Communication Register's already-computed style, reported and never re-decided.
5. **`whats_its_call`** — what is genuinely Jarvis's own decision. It reads the LIVE values out
   of `memory/policy.py`'s `THRESHOLDS` and `improvement/policy.py`'s `MIN_EVIDENCE_BY_TRUST`,
   and builds its sentences FROM those numbers, so the prose and the number are one read and
   cannot drift apart.

A goal is reported as *what Jarvis recorded it understood*, never as a verified account of what
the user meant. There is deliberately no computed "aligned / drifted" verdict: whether a goal
matches a request is a semantic question no plain code can honestly answer, so the model is
given both real texts to judge freshly.

## How results get recorded

Tool outcomes are recorded by subscribers on the event bus, never by a call from the turn loop
(`orchestrator/pipeline.py` deliberately imports nothing from `self/`, `improvement/`, `cost/`
or `observers/`):
- `observers/recording.py`'s `_record_tool_outcome` listens for `TOOL_COMPLETED` / `TOOL_FAILED`
  and calls `store.record_attempt()`.
- `_record_notable_tool_outcome` is a separate subscriber for `TOOL_FAILED`, `TOOL_REFUSED` and
  `TOOL_ESCALATED`, feeding `improvement/capture.py`'s `record_notable_tool_outcome()`. It has
  to be separate because a refused or parked call never reaches the executor's run step;
  `capabilities/execute.py` publishes `TOOL_REFUSED` / `TOOL_ESCALATED` at those two points so
  there is something to subscribe to.
- `record_attempt()` wraps its own errors and logs either branch to `capture_health`.

## Push and pull

- **Pull (live):** `tools/self_tools.py` provides `check_myself` (`core`, `meta`; builds the
  requested dimensions, saves a snapshot and returns `snapshotId` alongside them) and
  `track_goal` (records what Jarvis understands the current goal to be; no confirmation, since
  recording an understanding has no outward effect). `prompt.py`'s `SELF_KNOWLEDGE` instruction
  tells the model to call `check_myself` before claiming how reliable it is, what it is doing
  and why, or what is genuinely its call.
- **Push (defined, not wired):** `signals.detect()` and `prompt.py`'s `self_focus_section()`
  exist and are tested, but nothing in the turn loop currently computes the signals or emits the
  section. Note that `knownFailure` / `noTrackRecord` could only ever match a tool THIS turn had
  already used in an earlier step, since nothing has foreknowledge of what the model will call
  next — catching that before first use is the pull path's job.
