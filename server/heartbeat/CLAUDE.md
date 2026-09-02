# Heartbeat + Trigger + Proactive Attention (`server/heartbeat/*.js`)

See the root `CLAUDE.md`'s "Heartbeat" section for the decisions that matter beyond this
file (why the Interruption Broker needed generalizing, the reliability guarantees, the
quiet-hours/emergency design, the real dedup bug live testing caught). This file is the
module-by-module breakdown.

**Named for the user's own term for this mechanism — unrelated to `jobs/job-store.js`'s
`heartbeat_at` column**, which is worker-liveness tracking for a single running job, a
different concept entirely. Don't confuse the two while reading either directory.

## The chain

`schedule-store.js` (leaf) persists each registered item's own next-due time —
restart-safety depends on this being real, never in-memory. `sources/registry.js` (pure,
zero imports) is the entire plug-in surface: `registerSource({id, defaultIntervalMs,
listItems(), check(itemKey)})`. `engine.js` ticks once a minute, reconciles every
source's current item list against the schedule, then processes whatever's due
SEQUENTIALLY up to a per-tick cap — what turns a big restart catch-up into several ticks
of steady work instead of one burst, and what makes the `running` overlap guard
meaningful. `triggers.js` is the event-driven half, reacting to a job going
`awaiting_decision` immediately rather than waiting for the next poll — both paths
funnel into `engine.js`'s exported `routeFinding()`, the ONE place a finding becomes a
notification, an outbox row, and (Tier 1, available, not quiet-hours-blocked) real
proactive speech. `index.js`'s `startHeartbeat()` wires all of it, called once from
`server.js` beside the other three `start*()` calls.

## `outbox-store.js` — the generalized Interruption Broker

Owns the `outbox` table (db.js migration 13 — a real rebuild of the old job-only
`job_outbox`, not an ALTER, since SQLite can't relax a NOT NULL foreign key in place).
`source` (`'job'` | `'heartbeat'`) and `source_ref` are new; `job_id` stays its own
column, still real and still cascading, so every existing Jobs call site is unaffected.
**`jobs/job-store.js`'s own `addOutboxEntry`/`getOutboxForJob`/`listPendingOutbox`/
`markOutboxDelivered` are now thin wrappers over this file** (`source:'job'`,
`sourceRef:jobId` baked in) — Jobs' own code (`job-actions.js`, `worker.js`,
`orchestrator.js`, `server.js`'s job routes) needed zero changes to keep working
identically. `getPendingForSourceRef(source, sourceRef)` is the dedup lookup a source's
own routing should check before parking a second undelivered row for the same finding —
but see the next section for why this alone is NOT sufficient for every tier.

## The dedup bug live testing caught — read this before touching `check()` in a source

**A Tier 3 verdict never creates an outbox row at all** (see `engine.js`'s
`routeFinding()` — `if (verdict.tier === 3) return` happens right after the notification
is written, before any outbox insert). `outbox-store.js`'s dedup
(`getPendingForSourceRef`) only ever finds something to dedup against when a Tier 1/2
row exists and is still undelivered. **Confirmed live**, not hypothetical: the first
version of `jobs-source.js` had no dedup of its own at all, assuming `routeFinding()`'s
outbox-based check was enough — a job whose Tier 1 permission ask kept getting judged
Tier 3 by `decision.js` (a routine, low-stakes automation request, correctly not worth
interrupting for) produced a **fresh finding, a fresh spent `decision.js` model call, AND
a fresh notification on every single tick, forever**, since nothing ever recorded "this
exact ask was already reported." **The fix: a source with a persistent underlying
condition must track its OWN "have I already reported this" state via
`schedule-store.js`'s `checkState` (read with `getItem(sourceId, itemKey)`, written by
returning `{finding, checkState}` from `check()`), the same way `commitments-source.js`
already did for its own `notifiedApproaching`/`notifiedOverdue` flags.**
`jobs-source.js` now remembers the specific outbox entry id (`lastReportedOutboxId`) it
last reported and only fires again once that id actually changes — a genuinely new ask,
not the same still-unanswered one. **Any future source with a condition that can stay
true across many ticks needs this same discipline** — outbox-based dedup alone is only
ever real for Tier 1/2.

**The trigger path needed the identical fix, for a subtler reason.** `triggers.js`'s
reaction to a job's `awaiting_decision` transition calls the source's `check()` directly,
outside the normal tick — if it doesn't ALSO persist the `checkState` that call returns
(via `schedule-store.upsertItem()` + `markDone()`, the same two calls `engine.js`'s own
`processDueItem()` makes), the trigger's own dedup memory is silently lost, letting one
redundant re-fire slip through on the very next regular tick even though nothing had
changed. `triggers.js` does this explicitly now — see its own inline comment.

## `sources/jobs-source.js` and `sources/commitments-source.js`

Both leaf-adjacent, both registered from `index.js`. `jobs-source.js` (job-store.js +
outbox-store.js + schedule-store.js, no model calls of its own) closes the actual gap
this whole build exists for: a Tier 1 job outbox row sits completely silent —
structurally invisible to the user — until they happen to start a new conversation
themselves, at which point `prompt.js`'s drain finally surfaces it. This source turns
"sitting silently in the outbox" into a real Heartbeat finding. It deliberately does NOT
duplicate Jobs' own completion/failure notifications (already fired directly via
`addNotification()` in `worker.js`/`orchestrator.js`) — only the specific gap above.

`commitments-source.js` derives a deadline from an ordinary Memory row that has no
deadline column at all. Deterministic parsing (`date-parse.js`, zero dependencies, zero
quota) is the always-on first pass; a budgeted model call (`budget.js`, its own daily
ledger — never Self-Improvement's) is spent at most once per memory TEXT VERSION, only
when the parser came back empty. `date-parse.js`'s own header comment is explicit that
its coverage is a deliberately incomplete safety net, not a claim of completeness — the
model fallback is what closes the gap, not a second attempt at exhaustive regex coverage.

## `decision.js` — the one urgency-reasoning step

Used ONLY by Heartbeat/Trigger findings — Jobs' own tier assignment at its own call
sites (`worker.js`, `orchestrator.js`) is untouched and never calls this; those are
mechanical "does this need the owner" facts, not a judgment call. One model call
answers both the tier (1/2/3) AND — only when quiet hours are active — whether this
clears the emergency bar, to keep quota cost down. No model available, or an
unparseable reply, is NEVER treated as Tier 1 or an emergency by default; it falls back
to Tier 3. Silence is the safe failure direction in both places. Verified live against a
real model: an "overdrawn bank account in the next hour" finding correctly came back
Tier 1 (and, tested during quiet hours, `emergency:true` with a real stated reason);
an "overdue coffee filters" commitment correctly came back Tier 3 in both cases — and
the urgent verdict's own reasoning cited a real approved memory about the user's
finances, confirming the "weigh against known priorities" design is actually happening,
not just generic reasoning.

## `quiet-hours.js` / `presence.js` — the two gates on live speech

`quiet-hours.js` only answers "is it quiet right now," from `prefs.quietHours`
(`{enabled, start, end}` as 'HH:MM' strings, wraps midnight correctly). It gates LIVE
SPEECH only, in `engine.js`'s own routing — the notification and outbox row a finding
produces are unaffected either way, since they only take effect once the user is already
engaging, at which point quiet hours has nothing left to protect.

`presence.js` exports `isReachable()` (a tab is connected AND the user was recently
active — the hard requirement for any live delivery attempt) and `isBusy()` (the
secondary, skippable-for-emergencies dampener, via the same `control/ps-bridge.js`
window/process listing `server/monitor/engine.js` already uses) as two SEPARATE
functions, not one — an emergency verdict during quiet hours should still try to reach
the user through a detected "busy" state (the same way it already breaks through quiet
hours itself), while ordinary Tier 1 flow should respect both. `isAvailable()` combines
them for the ordinary case; the emergency path in `engine.js`'s `routeFinding()` calls
`isReachable()` alone.

## `speak.js` — the one genuinely new channel

Real proactive speech: pushes a real assistant message onto the active session
(`brain.js`'s `getActiveSessionId()` — reused deliberately rather than reimplemented, to
avoid drifting from the one authoritative place session resolution already lives) and
broadcasts a `proactive_message` SSE event any open tab can actually play. This is what
makes this file (and `engine.js`/`triggers.js`/`index.js` above it) NOT leaf-safe —
accepted, since nothing under `server/tools/` ever imports this file directly (only
`outbox-store.js`, a true leaf, for `acknowledge_notice.js`'s own needs).

## `prompt.js` / `acknowledge_notice.js`

`jobsSection()` (kept under its original name to avoid rippling a rename across every
comment that references it, despite draining more than jobs now) generalizes its own
drain to every `source`, wording a `source:'heartbeat'` row with a `notice_id` and a
different resolving instruction. `acknowledge_notice` (`core:true, meta:true`, no confirm
gate) is the one new tool this build needed — the resolving action a heartbeat-sourced
row has no equivalent of otherwise (a job's own tier1/2 rows resolve via
`check_on_work`/`stop_working_on` instead). Refuses outright for a `source:'job'` row,
so a model can't accidentally bypass Jobs' own resolution tools through this one.

## Verified live, not just by reading code

A full migration run against a real copy of the user's actual `jarvis.db` (12 real
`job_outbox` rows) confirmed every row survived the rebuild intact with
`source:'job'`/`source_ref` correctly backfilled, and the old table was really dropped.
A real scratch server (isolated data dir/port, the user's real `.env` for a working
model) confirmed: the tick's per-cap sequential processing genuinely spreads a 25-item
restart backlog across two ticks rather than one burst, with no item double-processed; a
broken source's `listItems()`/`check()` throwing never stopped a healthy source in the
same tick; and — the one real bug this pass actually found — the dedup gap described
above, caught by watching real notifications accumulate every ~3 minutes for the same
unresolved job before the fix, and confirmed gone after it.
