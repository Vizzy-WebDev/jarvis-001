# Operational Awareness — `server/ops/*.js`

See the root `CLAUDE.md`'s "Operational Awareness" section for the decisions that matter
beyond this file. This file is the module-by-module breakdown for `server/ops/` itself —
see `server/cost/CLAUDE.md` for the sibling Cost tracking build (items 2/6), which is a
separate directory on purpose (a genuinely different read/write shape, not operational
health).

**Status: items 1 (self-diagnosis + reasoning integrity), 4 (verification), and 5
(environment) are built and verified.** Item 4's semantic half (`verify.js`'s
`verifySemanticMatch()`) is wired at three of the four completion surfaces the original
spec named: artifact creation (`server/tools/create_artifact.js`, `run_code.js`), Job
completion (`jobs/worker.js`'s `turn.reportedDone` branch — see `server/jobs/CLAUDE.md`),
and scheduled-task outcomes (`scheduler.js`'s `runTaskNow()` — see
`server/scheduler/CLAUDE.md`). **Deliberately NOT wired into consequential CHAT
answers** — the costliest and most frequent of the four surfaces on a routinely
rate-limited roster; reasoning integrity (this same file, below) already added a
related but distinct check on chat turns (was `check_myself` consulted), and layering an
unconditional semantic-match model call onto every consequential reply is a separate,
larger cost decision this build does not make unilaterally. Still a disclosed, open item.

## `ops-trace.js` — the generic activity trace

Generalizes Jobs' own `job_trace` table (db.js migration 15) the exact same way
`heartbeat/outbox-store.js` generalized `job_outbox` -> `outbox` in migration 13: a real
rebuild (SQLite can't relax a NOT NULL foreign key in place), `job_id` now nullable,
`source`/`source_ref` added, every pre-existing row copied over as `source:'job',
source_ref:job_id`. `jobs/job-store.js`'s own `appendTrace`/`getTrace`/`getTraceTail`
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

- **`env-store.js`** (leaf) — `env_samples` (db.js migration 17), a rolling window
  pruned to 7 days (`pruneOld()`, called opportunistically by sampler.js every ~200
  samples, not on every write). An operational health signal, never a billing log — see
  root CLAUDE.md.
- **`sampler.js`** — real CPU/memory/Jarvis-own-memory sampling every 30s
  (`startSampling()`, called once from server.js). **Deliberately does not use
  `os.loadavg()`** — it returns `[0,0,0]` unconditionally on Windows, this project's own
  target platform, which would make every reading silently useless. Real CPU% instead
  comes from diffing two `os.cpus()` snapshots' idle/total time across the sampling
  interval — a single snapshot has no usable percentage in it (cumulative since boot).
  Verified live: a real busy-loop measurably moved the reading (idle ~0% -> a real
  double-digit CPU% over a 300ms synthetic load), and the very first sample after a
  restart is honestly `cpuPct:null` (no prior snapshot to diff against yet), never a
  fabricated zero. `sampleNow({record})` is the on-demand path `check_environment.js`
  uses for a "right now" reading without waiting for the next tick.
- **`reachability.js`** — a single read across three subsystems that each already track
  their own reachability fact, never a new probe of its own: models (`health.js`'s
  in-memory breaker AND `registry.js`'s persisted `availability.state`, reported as two
  separate fields on purpose — see root CLAUDE.md's Model system section on why those
  two are deliberately not collapsed into one), connectors (`connectors/store.js`'s own
  `status.state` — a stale record of the last human-triggered test, not a live probe),
  voice services (`tts/index.js`/`stt/deepgram.js`'s own `isConfigured()`).
- **`baseline.js`** — "unusually high or climbing without a clear cause," defined
  honestly rather than against a fixed number that would mean something different on
  every machine this ever runs on: a rolling median over the last hour of real samples,
  plus a hard **sustained-duration requirement** (5 continuous minutes, no gaps) before
  anything fires — the same "don't react to one blip" discipline
  `heartbeat/commitments-source.js`'s own notified-flags already established for a
  different noisy signal. **Verified live against controlled synthetic data**: a single
  90%-CPU sample two minutes ago, surrounded by an otherwise-normal hour, produces no
  finding at all; a genuine 6-minute continuous climb to ~78% against a ~16% baseline
  median correctly fires, with the real median and current value both in the summary
  text.
- **`source.js`** — the Heartbeat source (`registerSource`, `heartbeat/index.js`) that
  turns `baseline.js`'s check into a real finding. **Own-checkState dedup, same
  discipline `jobs-source.js` already established and `heartbeat/CLAUDE.md` documents at
  length as a real, previously-live bug** — a sustained-high-load condition can hold
  true across many ticks, so a finding only fires again when the reported KIND actually
  changes (nothing -> cpu, cpu -> memory, memory -> nothing), never on every tick the
  same ongoing condition is still true.

## `check_environment.js` / `check_myself.js` — the pull path

`server/tools/check_environment.js` (`core:true, meta:true`, read-only, no confirm
gate) is what lets a live conversation ask "can I actually do this right now" before
starting something — distinct from `check_myself.js`, which is about whether **Jarvis
itself** is malfunctioning, not the surrounding machine (see root CLAUDE.md's item 1 vs
item 5 distinction). Returns a live CPU/memory reading, the full reachability picture,
and `baseline.js`'s own anomaly verdict — never re-derives any of these itself.

## `diagnostics/` — item 1, self-diagnosis + self-heal

- **`registry.js`** (pure, zero-import) — the entire plug-in surface, deliberately the
  same shape as `heartbeat/sources/registry.js`: `registerCheck({id, intervalMs,
  probe(), remedy?()})`. `probe()` returns `{ok, detail?}`; `remedy()` is optional and
  best-effort.
- **`checks/*.js`** — nine real checks, none with a `remedy()` unless a genuinely safe
  automatic fix exists (only the synthetic test checks used to verify the mechanism
  itself have one; every real check here has no safe auto-remedy, so each escalates
  straight from a failed probe): `memory.js` (a real create->read->delete round trip
  into memory-store.js, fully SYNCHRONOUS with no `await` between the three calls — see
  its own header comment on why that makes the canary row structurally unobservable to
  anything else, not merely unlikely to be seen), `database.js` (`PRAGMA
  integrity_check`), `jobs.js`/`scheduler.js` (a queued job or overdue task that COULD
  run right now — capacity/resource free — but hasn't, meaning the Orchestrator's or
  Scheduler's own tick may have silently stopped; correctly does NOT flag a job/task
  that's legitimately blocked on capacity), `capture-health.js` (reuses
  `self-store.js`'s own `captureHealthSummary()` rather than re-deriving it — a MAJORITY
  failure rate, not merely >0 failures, is the signal), `voice.js` (server-side
  reachability only — the browser half is invisible from here, same honest limit
  `environment/reachability.js` states), and three security checks (below). **A real
  bug in this check's own first version was found and fixed by a concurrent session
  working in this same repo, not this build's own testing**: it called
  `tts.isConfigured()` with no argument, which resolves via `prefs.ttsProvider` — a pref
  nothing in this codebase ever writes — so the check was structurally unable to ever
  pass regardless of what the user actually had configured, confirmed live reporting
  "no TTS provider configured" on every tick even with a real, working voice service
  saved. Fixed to read `tts.listProviders().some(p => p.configured)` instead — whether
  ANY configured external service actually resolves, the real question this check
  exists to answer.
- **`checks/security/*.js`** — detection only, per the owner's own explicit scope; NONE
  of these declare a `remedy()`, on purpose. `config-integrity.js` watches `.env`,
  `connections.json`, `external-services.json`, `connectors.json` for a change with no
  matching in-app write — see its own extensive header comment for a real,
  live-caught bug: a TIME-WINDOW version of "was this explained by our own recent
  write" was tried first and found genuinely wrong (a legitimate write's timestamp
  stays "recent" long enough to wrongly excuse a LATER, unrelated external change in
  the same window) — fixed by comparing the actual CONTENT HASH the app itself last
  wrote (`store.js`'s `lastWriteAt()`/`config.js`'s `envLastWriteTime()`, both
  in-memory, per-process) rather than a timestamp. `event-spikes.js` watches for a
  SPIKE (a fixed count-in-window threshold, not a median — a rare discrete event has no
  meaningful "typical" to compare against) of real auth failures
  (`models/health.js`'s `markUnhealthy(..., 'auth')`) or same-turn confirm-gate bypass
  attempts (`capabilities.js`'s `consumePendingToken()`) — both write into
  `security-counters.js`'s own `ops_security_events` table (migration 18) via a
  one-directional leaf dependency (health.js/capabilities.js call it; it never imports
  either back). `listeners.js` watches for a genuinely NEW local TCP listener via
  PowerShell's `Get-NetTCPConnection` (never `netstat` text parsing), first-seen-is-
  baseline the same way `config-integrity.js` establishes its own hash baseline.
  **Never fires on this app's own normal work, including a concurrent Claude session
  editing SOURCE** — these three checks watch only DATA/secret files, network listeners,
  and two specific counted events; nothing here ever reads `server/` or `public/`.
- **`source.js`** — the Heartbeat source running every registered check.
  **Retry-then-escalate, the SAME shape Jobs already built, genuinely reused via
  `checkState.lastOutcome` rather than a numeric counter**: a brand-new failure (was
  `'ok'` last tick) gets exactly one `remedy()` attempt (if declared) then a re-probe;
  success clears state silently (still logged to `ops_trace`, just not escalated); a
  still-failing re-probe (or no remedy at all) escalates with a real finding. A
  STILL-failing check on the NEXT tick (already `'failing'`) does not retry again and
  does not re-report — the same own-checkState dedup discipline every other Heartbeat
  source in this project uses. **Verified with a synthetic self-healing check AND a
  synthetic remedy-fails check** (registry.js's real production dispatch, not a mock of
  it) — both the silent-heal and the escalate paths confirmed correct, including the
  real `ops_trace` rows each leaves behind.
- **`index.js`** — registers all nine checks; called once from `heartbeat/index.js`,
  before `registerSource(diagnosisSource)`, so the source's very first tick has real
  checks to run.

## `check_my_health.js` (`server/tools/`) — the pull path for item 1

`core:true, meta:true`, read-only, no confirm gate. Runs every registered check LIVE,
right now (not cached), plus the last 20 real `ops_trace` rows for `source:'diagnosis'`
— "has anything gone wrong with you lately?" reads real history, never a vague claim.
Distinct from `check_myself.js` (Self-Model's own reliability/provenance/goal
dimensions — nothing about malfunction or possible compromise) and `check_environment`
(the surrounding MACHINE, not Jarvis itself).

## `verify.js` — item 4, Verification/Quality Control

Two tiers, per the owner's own explicit spec. **Mechanical** (`verifyFileOpens()`,
`verifyCodeRan()`) always runs, zero model calls — a generated file is re-read through
this project's OWN real, independently-built readers (`documents/index.js`'s
`extractDocument()` for Office formats — the exact discipline the artifact writers
themselves were verified with), `JSON.parse` for JSON, a `<svg>` root check for SVG,
non-empty for everything else; a sandboxed code run's own real exit code. **Semantic**
(`verifySemanticMatch()`, one budgeted model call — "does this genuinely answer what was
asked") is wired at three completion surfaces. **No new recovery mechanism, by design**
— a verification failure spends the SAME `canAutoRetry`-shaped single retry, then
escalates through the SAME trace -> `awaiting_decision` -> tier-1 outbox -> notification
path Jobs already built; this file supplies the CHECK, never a second recovery loop.

**Wired at artifact creation** (`server/artifacts/CLAUDE.md`'s own `create_artifact.js`/
`run_code.js` sections) — mechanical, always, and the one real bug this build's own
testing caught before shipping (a passing check was never actually RECORDED, leaving
`artifacts.verified` permanently null even for a kept, working file) is fixed and
re-verified.

**Wired at Job completion** (`jobs/worker.js`'s `turn.reportedDone` branch — see
`server/jobs/CLAUDE.md`'s own `worker.js` section for the full account) — genuinely
reuses `canAutoRetry`/escalate at the SAME call site stall-detection already uses it,
sharing the SAME `job.retries` counter, never a second mechanism. Verified via a real
stub model through the real `driveJob()` path: a job whose own summary never matches
its goal retries once then escalates with real trace/outbox rows; a job that corrects
itself on the retry finishes normally; a genuinely matching job finishes immediately
with no extra noise — all three confirmed via real database rows, not just reading the
code.

**Wired at scheduled-task outcomes** (`scheduler.js`'s `runTaskNow()`, `prompt` actions
only — see `server/scheduler/CLAUDE.md`) — no existing retry loop here to reuse or
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
booted end to end against the real `server.js` and confirmed to migrate cleanly through
every migration to `user_version 19`, with `check_environment`/`check_spending`/
`check_my_health`/`create_artifact` all present in `tools/index.js`'s own startup load
log — proof none tripped the
loader/runner circular-import invariant, not just an assumption from reading the import
graph. A separate run migrated a real copy of the user's own `jarvis.db` and confirmed
every pre-existing `job_trace` row (156 of them) survived the `trace` generalization
byte-for-byte.
