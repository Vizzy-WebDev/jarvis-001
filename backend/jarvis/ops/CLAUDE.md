# Operational Awareness — `jarvis/ops/`

See the root `CLAUDE.md`'s "Operational Awareness" section for the decisions that matter
beyond this file, and `jarvis/cost/CLAUDE.md` for the sibling cost-tracking package (a
different read/write shape, so a separate directory on purpose).

`ops/` answers one question: **is Jarvis working RIGHT NOW** — is anything broken, can the
machine take this on, and did what it just produced hold up. Its neighbours answer different
ones: `jarvis/self/` is what Jarvis IS and can do, `jarvis/improvement/` is what it has
LEARNED. Everything here plugs into the heartbeat (`jarvis/heartbeat/CLAUDE.md`) rather than
owning a timer of its own, apart from the environment sampler.

## `trace.py` — the write-ahead activity trace

An `intent` row goes down BEFORE an effectful action and an `outcome` row after, so a crash
between the two still leaves the intent on record — the basis on which recovery is honest
rather than declared. It is shared, not job-specific: rows carry a `source` and `source_ref`
(`job_id` is nullable), and `seq` is scoped to `(source, source_ref)` so one subsystem's
numbering can never be perturbed by another's writes. API: `append()`, `read()`, `tail()`,
`recent(source, since_iso=...)`. `jobs/job_store.py`'s trace functions are thin wrappers with
`source='job'` bound.

## `environment/` — live system awareness

- `sampler.py` — real CPU, free memory and this process's own footprint via `psutil`, every
  `SAMPLE_INTERVAL_S` (30s), gated by `JARVIS_ENV_SAMPLER`. The first CPU reading is honestly
  `None` (a percentage is a measurement over an interval), and the baseline ignores it.
  `read_now()` is the on-demand path `tools/check_environment.py` uses.
- `store.py` — a leaf over `env_samples`, a rolling window (`prune_old()`). An operational
  health signal, never a billing log.
- `baseline.py` — "unusually high, or climbing", defined against THIS machine: this machine's
  own median over the last hour, and a hard sustained duration (`SUSTAINED`, 5 minutes)
  before anything fires. Pure: it returns a finding or `None`.
- `reachability.py` — a single read across subsystems that already track their own answer
  (connectors' stored status, voice services; models report whether the selected model resolves to a
  connection that has what it needs, read from the selection and never by calling a provider). It never probes
  anything itself, since a second differently-timed opinion would disagree exactly when it
  mattered.
- `source.py` — the heartbeat source for load. Keeps its own `checkState` dedup: a finding
  fires only when the KIND of anomaly changes (nothing → cpu → memory → nothing), never on
  every tick a sustained condition stays true.
- `tools/check_environment.py` (`core`, `meta`, read-only) is the pull path: a live reading,
  the reachability picture and the baseline's verdict, never re-derived.

## `diagnostics/` — self-diagnosis and one automatic fix attempt

- `registry.py` (pure, zero imports) — the whole plug-in surface: `register_check({id,
  intervalMs, probe(), remedy?()})`. `probe()` returns `{ok, detail?}` with a plain-language
  `detail`. `remedy()` is optional and best-effort.
- `checks/__init__.py` — the one place the full list lives (`register_all()`, nine checks).
  `heartbeat/engine.py` calls it before registering the diagnosis source so its first tick has
  real checks.
- `source.py` — runs the checks. **One remedy per NEW failure, never per tick:** an
  ok→failing transition spends one `remedy()` (if any) then re-probes; success clears state
  silently, a still-failing re-probe escalates with a finding; a check already `failing` on
  the next tick does neither. State is the schedule row's own `checkState`. Every probe,
  remedy and outcome writes a trace row (`source='diagnosis'`), which is what makes "has
  anything gone wrong with you lately?" a real answer.
- Checks: `memory` (a fully synchronous create→read→delete canary, so nothing else can
  observe the row), `database` (a write/read round trip, not a connection test), `jobs`
  (is the SUPERVISOR running — a `running` job with a heartbeat far past the hang timeout;
  its remedy is one supervision pass), `scheduler` (an overdue task that could run but has not;
  only meaningful when the scheduler interlock is on), `capture_health` (is the recorder in
  `self/` still working), `voice` (the wake word's package can be found, not that its model
  loads).
- `checks/security/` — **detection only: none declares a `remedy()` and the registry refuses
  one.** A build that quietly "fixes" a security anomaly can destroy the evidence.
  `config_integrity` detects a change to the secrets/config files with no matching in-app
  write, by comparing the CONTENT HASH of what this app last wrote (a time-window version
  wrongly excused later unrelated changes) and never reporting file contents. `event_spikes`
  detects a burst of failed capability calls or refused approvals against this install's own
  history (counted in `ops_security_events` via `counters.py`; `observers/security.py` does
  the recording). `listeners` flags a listening socket owned by THIS process on any address
  other than `127.0.0.1`. None of them read `jarvis/` source.
- `tools/check_my_health.py` (`core`, `meta`, read-only) runs every registered check LIVE plus
  the last 20 `diagnosis` trace rows. It is distinct from `tools/self_tools.py` (the
  Self-Model) and `check_environment` (the surrounding machine).

## `verify.py` — Verification

Two tiers. **Mechanical** (`verify_file_opens()`, `verify_code_ran()`) always runs and costs no
model call: a generated file is re-read through the project's own readers (`documents/`), JSON
must parse, SVG needs an `<svg>` root, a sandboxed run's exit code is checked. **Semantic**
(`verify_semantic_match()`) is one budgeted model call asking whether the result genuinely
answers the request. **"Could not check" is never a pass or a fail:** no model, or an
unreadable reply, returns `checked=False`.

**No new recovery mechanism, by design** — the check supplies a verdict and each caller uses
the retry-then-escalate it already has. Semantic verification is wired at:
- **Artifacts** — mechanical, via `artifacts/store.py`'s `keep()` (`jarvis/artifacts/CLAUDE.md`).
- **Job completion** — `jobs/worker.py`'s `_verify_result()`. A checked mismatch is treated
  exactly like a stall: the same single retry, the same counter, the same escalation.
- **Scheduled tasks** — `scheduler/engine.py`'s `_verify_run()`, `prompt` actions only.
- **Consequential chat answers** — `observers/verification.py`, on the event bus (the turn loop
  never imports verification). It runs AFTER the reply, off-thread, so it records a mismatch
  instead of delaying anything. Off by default (`prefs.verifyChatAnswers`), and
  `consequence.py` bounds the cost: `is_consequential()` is pure and conservative (produced a
  file, used a non-trivial tool, or a 200+ character answer), and `DAILY_BUDGET` caps it at 20
  calls a day.
