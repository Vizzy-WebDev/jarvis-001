<!-- Ported from the Node build during the S6 cutover. The architecture, the
invariants and the live-caught bugs described here all carried over deliberately and
still hold. File paths have been updated to their real Python counterparts and are
verified to exist. Function names written in camelCase (`getToolDeclarations()`) are
the NODE originals, kept because the surrounding reasoning is about them; the Python
equivalent is the snake_case function doing that job in the same module. Where a Node
module had no Python counterpart, the text says so rather than pointing at a file that
does not exist. -->

# Operational Awareness — `jarvis/ops/*.py`

See the root `CLAUDE.md`'s "Operational Awareness" section for the decisions that matter
beyond this file. This file is the module-by-module breakdown for `jarvis/ops/` itself —
see `jarvis/cost/CLAUDE.md` for the sibling Cost tracking build (items 2/6), which is a
separate directory on purpose (a genuinely different read/write shape, not operational
health).

**Status: items 1 (self-diagnosis + reasoning integrity), 4 (verification), and 5
(environment) are built and verified.** Item 4's semantic half (`verify.py`'s
`verifySemanticMatch()`) is wired at three of the four completion surfaces the original
spec named: artifact creation (`jarvis/tools/create_artifact.py`, `run_code.py`), Job
completion (`jobs/worker.py`'s `turn.reportedDone` branch — see `jarvis/jobs/CLAUDE.md`),
and scheduled-task outcomes (`scheduler/engine.py`'s `runTaskNow()` — see
`jarvis/scheduler/CLAUDE.md`). **Deliberately NOT wired into consequential CHAT
answers** — the costliest and most frequent of the four surfaces on a routinely
rate-limited roster; reasoning integrity (this same file, below) already added a
related but distinct check on chat turns (was `check_myself` consulted), and layering an
unconditional semantic-match model call onto every consequential reply is a separate,
larger cost decision this build does not make unilaterally. Still a disclosed, open item.

## `ops/trace.py` — the generic activity trace

Generalizes Jobs' own `job_trace` table (db.py migration 15) the exact same way
`heartbeat/outbox.py` generalized `job_outbox` -> `outbox` in migration 13: a real
rebuild (SQLite can't relax a NOT NULL foreign key in place), `job_id` now nullable,
`source`/`source_ref` added, every pre-existing row copied over as `source:'job',
source_ref:job_id`. `jobs/job_store.py`'s own `appendTrace`/`getTrace`/`getTraceTail`
are now thin wrappers over this file with `source:'job'` baked in — every existing Jobs
call site needed zero changes, verified against a real copy of the user's own database
(all 156 pre-existing rows survived with correct `source`/`source_ref`, `classifyRecovery`/
`diagnoseStall` both still worked, cascade-delete still worked).

`appendEntry({source, sourceRef, jobId, phase, effect, kind, summary, detail})` scopes
its own `seq` counter by `(source, sourceRef)` — a non-job source (a self-diagnosis
check, a verification run) gets its own independent sequence space, verified live to
never cross-contaminate a job's own numbering. `listRecentForSource(source, {limit})` is
what a future "has anything gone wrong with you lately?" answer reads across every
`source_ref` for one source, capped, never a full-table scan.

## `environment/` — item 5, live system/service awareness

- **`ops/trace.py`** (leaf) — `env_samples` (db.py migration 17), a rolling window
  pruned to 7 days (`pruneOld()`, called opportunistically by ops/diagnostics/ every ~200
  samples, not on every write). An operational health signal, never a billing log — see
  root CLAUDE.md.
- **`ops/diagnostics/`** — real CPU/memory/Jarvis-own-memory sampling every 30s
  (`startSampling()`, called once from main.py). **Deliberately does not use
  `os.loadavg()`** — it returns `[0,0,0]` unconditionally on Windows, this project's own
  target platform, which would make every reading silently useless. Real CPU% instead
  comes from diffing two `os.cpus()` snapshots' idle/total time across the sampling
  interval — a single snapshot has no usable percentage in it (cumulative since boot).
  Verified live: a real busy-loop measurably moved the reading (idle ~0% -> a real
  double-digit CPU% over a 300ms synthetic load), and the very first sample after a
  restart is honestly `cpuPct:null` (no prior snapshot to diff against yet), never a
  fabricated zero. `sampleNow({record})` is the on-demand path `check_environment.py`
  uses for a "right now" reading without waiting for the next tick.
- **`ops/diagnostics/checks/`** — a single read across three subsystems that each already track
  their own reachability fact, never a new probe of its own: models (`gateway/availability.py`,
  which is the one store this build keeps that fact in — the Node original had an in-memory
  breaker AND a persisted state on the model row, and the two could disagree), connectors (`connectors/store.py`'s own
  `status.state` — a stale record of the last human-triggered test, not a live probe),
  voice services (`tts/matching.py`/`stt/deepgram.py`'s own `isConfigured()`).
- **`ops/diagnostics/`** — "unusually high or climbing without a clear cause," defined
  honestly rather than against a fixed number that would mean something different on
  every machine this ever runs on: a rolling median over the last hour of real samples,
  plus a hard **sustained-duration requirement** (5 continuous minutes, no gaps) before
  anything fires — the same "don't react to one blip" discipline
  `heartbeat/heartbeat/triggers.py`'s own notified-flags already established for a
  different noisy signal. **Verified live against controlled synthetic data**: a single
  90%-CPU sample two minutes ago, surrounded by an otherwise-normal hour, produces no
  finding at all; a genuine 6-minute continuous climb to ~78% against a ~16% baseline
  median correctly fires, with the real median and current value both in the summary
  text.
- **`heartbeat/triggers.py`** — the Heartbeat source (`registerSource`, `heartbeat/engine.py`) that
  turns `ops/diagnostics/`'s check into a real finding. **Own-checkState dedup, same
  discipline `heartbeat/triggers.py` already established and `heartbeat/CLAUDE.md` documents at
  length as a real, previously-live bug** — a sustained-high-load condition can hold
  true across many ticks, so a finding only fires again when the reported KIND actually
  changes (nothing -> cpu, cpu -> memory, memory -> nothing), never on every tick the
  same ongoing condition is still true.

## `check_environment.py` / `tools/self_tools.py` — the pull path

`jarvis/tools/check_environment.py` (`core:true, meta:true`, read-only, no confirm
gate) is what lets a live conversation ask "can I actually do this right now" before
starting something — distinct from `tools/self_tools.py`, which is about whether **Jarvis
itself** is malfunctioning, not the surrounding machine (see root CLAUDE.md's item 1 vs
item 5 distinction). Returns a live CPU/memory reading, the full reachability picture,
and `ops/diagnostics/`'s own anomaly verdict — never re-derives any of these itself.

## `diagnostics/` — item 1, self-diagnosis + self-heal

- **`registry.py`** (pure, zero-import) — the entire plug-in surface, deliberately the
  same shape as `heartbeat/triggers.py`: `registerCheck({id, intervalMs,
  probe(), remedy?()})`. `probe()` returns `{ok, detail?}`; `remedy()` is optional and
  best-effort.
- **`checks/*.py`** — nine real checks, none with a `remedy()` unless a genuinely safe
  automatic fix exists (only the synthetic test checks used to verify the mechanism
  itself have one; every real check here has no safe auto-remedy, so each escalates
  straight from a failed probe): `routes/memory.py` (a real create->read->delete round trip
  into memory/store.py, fully SYNCHRONOUS with no `await` between the three calls — see
  its own header comment on why that makes the canary row structurally unobservable to
  anything else, not merely unlikely to be seen), `db.py` (`PRAGMA
  integrity_check`), `routes/jobs.py`/`scheduler/engine.py` (a queued job or overdue task that COULD
  run right now — capacity/resource free — but hasn't, meaning the Orchestrator's or
  Scheduler's own tick may have silently stopped; correctly does NOT flag a job/task
  that's legitimately blocked on capacity), `self/store.py` (reuses
  `self/store.py`'s own `captureHealthSummary()` rather than re-deriving it — a MAJORITY
  failure rate, not merely >0 failures, is the signal), `routes/voice.py` (server-side
  reachability only — the browser half is invisible from here, same honest limit
  `ops/diagnostics/checks/` states), and three security checks (below). **A real
  bug in this check's own first version was found and fixed by a concurrent session
  working in this same repo, not this build's own testing**: it called
  `tts.isConfigured()` with no argument, which resolves via `prefs.ttsProvider` — a pref
  nothing in this codebase ever writes — so the check was structurally unable to ever
  pass regardless of what the user actually had configured, confirmed live reporting
  "no TTS provider configured" on every tick even with a real, working voice service
  saved. Fixed to read `tts.listProviders().some(p => p.configured)` instead — whether
  ANY configured external service actually resolves, the real question this check
  exists to answer.
- **`checks/security/*.py`** — detection only, per the owner's own explicit scope; NONE
  of these declare a `remedy()`, on purpose. `ops/diagnostics/checks/` watches `.env`,
  `connections.json`, `external-services.json`, `connectors.json` for a change with no
  matching in-app write — see its own extensive header comment for a real,
  live-caught bug: a TIME-WINDOW version of "was this explained by our own recent
  write" was tried first and found genuinely wrong (a legitimate write's timestamp
  stays "recent" long enough to wrongly excuse a LATER, unrelated external change in
  the same window) — fixed by comparing the actual CONTENT HASH the app itself last
  wrote (`store.py`'s `lastWriteAt()`/`config.py`'s `envLastWriteTime()`, both
  in-memory, per-process) rather than a timestamp. `observers/security.py` watches for a
  SPIKE (a fixed count-in-window threshold, not a median — a rare discrete event has no
  meaningful "typical" to compare against) of real auth failures
  (`gateway/availability.py`'s `record(..., 'auth')`) or same-turn confirm-gate bypass
  attempts (`capabilities/`'s `consumePendingToken()`) — both write into
  `observers/security.py`'s own `ops_security_events` table (migration 18) via a
  one-directional leaf dependency (gateway/availability.py and capabilities/registry.py call it; it never imports
  either back). `events/bus.py` watches for a genuinely NEW local TCP listener via
  PowerShell's `Get-NetTCPConnection` (never `netstat` text parsing), first-seen-is-
  baseline the same way `ops/diagnostics/checks/` establishes its own hash baseline.
  **Never fires on this app's own normal work, including a concurrent Claude session
  editing SOURCE** — these three checks watch only DATA/secret files, network listeners,
  and two specific counted events; nothing here ever reads `jarvis/` or `public/`.
- **`heartbeat/triggers.py`** — the Heartbeat source running every registered check.
  **Retry-then-escalate, the SAME shape Jobs already built, genuinely reused via
  `checkState.lastOutcome` rather than a numeric counter**: a brand-new failure (was
  `'ok'` last tick) gets exactly one `remedy()` attempt (if declared) then a re-probe;
  success clears state silently (still logged to `ops_trace`, just not escalated); a
  still-failing re-probe (or no remedy at all) escalates with a real finding. A
  STILL-failing check on the NEXT tick (already `'failing'`) does not retry again and
  does not re-report — the same own-checkState dedup discipline every other Heartbeat
  source in this project uses. **Verified with a synthetic self-healing check AND a
  synthetic remedy-fails check** (registry.py's real production dispatch, not a mock of
  it) — both the silent-heal and the escalate paths confirmed correct, including the
  real `ops_trace` rows each leaves behind.
- **`__init__.py`** — registers all nine checks; called once from `heartbeat/engine.py`,
  before `registerSource(diagnosisSource)`, so the source's very first tick has real
  checks to run.

## `check_my_health.py` (`jarvis/tools/`) — the pull path for item 1

`core:true, meta:true`, read-only, no confirm gate. Runs every registered check LIVE,
right now (not cached), plus the last 20 real `ops_trace` rows for `source:'diagnosis'`
— "has anything gone wrong with you lately?" reads real history, never a vague claim.
Distinct from `tools/self_tools.py` (Self-Model's own reliability/provenance/goal
dimensions — nothing about malfunction or possible compromise) and `check_environment`
(the surrounding MACHINE, not Jarvis itself).

## `verify.py` — item 4, Verification/Quality Control

Two tiers, per the owner's own explicit spec. **Mechanical** (`verifyFileOpens()`,
`verifyCodeRan()`) always runs, zero model calls — a generated file is re-read through
this project's OWN real, independently-built readers (`documents/reader.py`'s
`extractDocument()` for Office formats — the exact discipline the artifact writers
themselves were verified with), `JSON.parse` for JSON, a `<svg>` root check for SVG,
non-empty for everything else; a sandboxed code run's own real exit code. **Semantic**
(`verifySemanticMatch()`, one budgeted model call — "does this genuinely answer what was
asked") is wired at three completion surfaces. **No new recovery mechanism, by design**
— a verification failure spends the SAME `canAutoRetry`-shaped single retry, then
escalates through the SAME trace -> `awaiting_decision` -> tier-1 outbox -> notification
path Jobs already built; this file supplies the CHECK, never a second recovery loop.

**Wired at artifact creation** (`jarvis/artifacts/CLAUDE.md`'s own `create_artifact.py`/
`run_code.py` sections) — mechanical, always, and the one real bug this build's own
testing caught before shipping (a passing check was never actually RECORDED, leaving
`artifacts.verified` permanently null even for a kept, working file) is fixed and
re-verified.

**Wired at Job completion** (`jobs/worker.py`'s `turn.reportedDone` branch — see
`jarvis/jobs/CLAUDE.md`'s own `worker.py` section for the full account) — genuinely
reuses `canAutoRetry`/escalate at the SAME call site stall-detection already uses it,
sharing the SAME `job.retries` counter, never a second mechanism. Verified via a real
stub model through the real `driveJob()` path: a job whose own summary never matches
its goal retries once then escalates with real trace/outbox rows; a job that corrects
itself on the retry finishes normally; a genuinely matching job finishes immediately
with no extra noise — all three confirmed via real database rows, not just reading the
code.

**Wired at scheduled-task outcomes** (`scheduler/engine.py`'s `runTaskNow()`, `prompt` actions
only — see `jarvis/scheduler/CLAUDE.md`) — no existing retry loop here to reuse or
collide with, so a mismatch simply flips `result.ok` to `false` and folds the reason
into `error`; the task's own next scheduled occurrence is its natural retry cadence.
Verified via a real stub model through the real `runTaskNow()` path in both directions.

**Deliberately NOT wired into consequential CHAT answers** — the costliest and most
frequent of the four surfaces the original spec named, on a routinely rate-limited
roster (see root CLAUDE.md's Gotchas). Reasoning integrity (this same file, above)
already added a related but distinct check on chat turns (was `check_myself`
consulted); layering an unconditional semantic-match model call onto every consequential
chat reply is a separate, larger cost decision this build does not make unilaterally.
Still a real, disclosed, open item — see root `CLAUDE.md`'s Operational Awareness
section for the same note.

## Verified, not just read

A completely fresh install (no prior data at all, not a copy of the real database) was
booted end to end against the real `main.py` and confirmed to migrate cleanly through
every migration to `user_version 19`, with `check_environment`/`check_spending`/
`check_my_health`/`create_artifact` all present in `capabilities/registry.py`'s own startup load
log — proof none tripped the
loader/runner circular-import invariant, not just an assumption from reading the import
graph. A separate run migrated a real copy of the user's own `jarvis.db` and confirmed
every pre-existing `job_trace` row (156 of them) survived the `trace` generalization
byte-for-byte.
