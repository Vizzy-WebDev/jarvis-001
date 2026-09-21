# Heartbeat + Trigger + Proactive Attention (`jarvis/heartbeat/`)

See the root `CLAUDE.md`'s "Heartbeat" section for the decisions that matter beyond this
file (why the Interruption Broker was generalized, the reliability guarantees, the
quiet-hours/emergency design). This file is the module-by-module breakdown.

**Unrelated to `jobs/job_store.py`'s `heartbeat_at` column**, which is worker-liveness
tracking for a single running job. Don't confuse the two.

## The chain

- `sources/registry.py` (pure, no imports) is the whole plug-in surface. A `Source` has an
  `id`, a `default_interval_ms`, `list_items()` (what it is watching, as `{itemKey,
  intervalMs?}`) and `check(item_key)` returning `{finding, checkState}`. A new source is
  one `register()` call.
- `schedule_store.py` (leaf) persists each item's own next-due time, so restart-safety is
  real and never in-memory.
- `engine.py` ticks every `TICK_SECONDS` (60), gated by `JARVIS_HEARTBEAT`. `reconcile()`
  syncs each source's current items against the schedule; `tick()` then processes what is due
  SEQUENTIALLY up to `PER_TICK_CAP` (20), which turns a big restart backlog into several
  ticks of steady work instead of one burst of model calls. A source whose `list_items()`
  throws is skipped for the tick; an item whose `check()` throws still advances its schedule
  (in a `finally`), so a consistently failing check does not retry on every tick forever.
  `register_default_sources()` is the one place that lists what is watched by default:
  jobs, commitments, the environment source, and the diagnosis source (its checks are
  registered first so the first tick has real checks).
- `triggers.py` is the event-driven half: it subscribes to the event bus and reacts to a job
  going `awaiting_decision` immediately, instead of waiting for the next poll. Both entry
  points feed the same `route_finding()` — one pipeline, so "should this interrupt someone"
  is decided in exactly one place. Started from `assembly.start_background_work()`.
- **`route_finding()`** is the ONE place a finding becomes a notification, an outbox row
  and, for Tier 1 when allowed, real proactive speech. Order: skip if an undelivered row for
  that `source_id:item_key` already exists → `decide_attention()` → publish a
  `NOTIFICATION_CREATED` event (the durable record, written before any delivery is
  considered) → Tier 3 stops there → otherwise add an outbox row → Tier 1 speaks only if
  `_may_speak_now()`.

## `outbox.py` — the Interruption Broker

Owns the `outbox` table (`source` is `job` or `heartbeat`, plus `source_ref`; `job_id` stays
its own cascading column). `jobs/job_store.py`'s outbox functions are thin wrappers over this
file with `source='job'` bound. `pending_for_source_ref()` is the dedup lookup `route_finding()`
uses — but it is NOT sufficient by itself, see the next section. `orchestrator/context.py`
drains pending Tier 1/2 rows into the next turn the user starts.

## Dedup: read this before touching `check()` in a source

**A Tier 3 verdict never creates an outbox row**, so outbox-based dedup only ever finds
something for Tier 1/2. A source whose condition can stay true across many ticks — a job whose
permission ask keeps being judged Tier 3, say — would otherwise produce a fresh finding, a
fresh spent model call AND a fresh notification on every tick, forever. This happened live.

**The rule: such a source tracks its OWN "already reported" state in `checkState`** (read
back with `schedule_store.get_item()`, written by returning `{finding, checkState}` from
`check()`). `jobs_source.py` remembers the specific outbox entry id it last reported and only
fires again when that id changes; `commitments_source.py` keeps its own approaching/overdue
flags. The trigger path needs the same care: it calls the source's `check()` outside the
normal tick, so it must ALSO persist the returned `checkState` (`upsert_item()` +
`mark_done()`) or its dedup memory is silently lost and one redundant re-fire slips through.

## Sources

- `sources/jobs_source.py` closes the real gap this subsystem exists for: a Tier 1 outbox
  row otherwise sits silent until the user happens to start a new conversation. It does NOT
  duplicate Jobs' own completion/failure notifications, only that gap. A cheap database read,
  so it runs every 3 minutes.
- `sources/commitments_source.py` watches memories carrying a real `expiresAt` — the
  structured field extraction fills in for things that stop being true on a known date — and
  notices two moments: shortly before, and once it has passed. It is deliberately built on the
  structured field rather than parsing dates out of prose.
- The environment and diagnosis sources live in `ops/` (see `ops/CLAUDE.md`).

## `decision.py` — the one urgency-reasoning step

`decide_attention(finding)` is used ONLY by Heartbeat/Trigger findings. Jobs' own tier
assignment is mechanical and never calls it. One model call answers the tier (1/2/3) and,
only when quiet hours are active, whether this clears the emergency bar. No model, an
unparseable reply, or an exception is NEVER treated as Tier 1 or an emergency: it falls back to
Tier 3. Silence is the safe failure direction.

## `quiet_hours.py` / `presence.py` — the two gates on live speech

`quiet_hours.is_quiet_now()` reads `prefs.quietHours` (`{enabled, start, end}` as `HH:MM`,
wrapping midnight). It gates LIVE SPEECH only — the notification and outbox row a finding
produces are unaffected either way. `presence.py` exports `is_reachable()` (a tab is connected
and the user was recently active — required for any live delivery) and `is_busy()` (a
skippable-for-emergencies dampener based on running processes) as SEPARATE functions;
`is_available()` combines them for the ordinary case. In quiet hours an emergency skips the
quiet gate and the busy dampener but never reachability (`_may_speak_now()`).

## `speak.py` — the one genuinely new channel

`speak_now()` starts a turn nobody asked for: a real assistant message on the active session
plus an event any open tab can play aloud, and the outbox row is marked delivered, since
actually saying it IS the resolving action for this path. The next-turn fallback is different:
being shown to the model is not the same as having been said, so that row is marked only when
`tools/acknowledge_notice.py` (`acknowledge_notice`, for heartbeat-sourced notices) or a Jobs
tool (`check_on_work`, `stop_working_on`) acts on it. `acknowledge_notice` refuses a
job-sourced row, so it cannot bypass Jobs' own resolution tools.

Because it touches the session and the bus, `speak.py`, `engine.py` and `triggers.py` are not
leaf-safe; nothing under `jarvis/tools/` may import them (tools reach only `outbox.py`).
