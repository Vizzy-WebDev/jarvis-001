# Handoff — read this first when picking this project back up

For full session narratives, see **`handoff-archive.md`**. This file is
current-state only — kept short on purpose so a cold session actually reads
it end to end. See "The pruning rule" at the bottom before adding to it.

## Right now

**Most recent session (2026-09-02, later the same day again): built the Heartbeat +
Trigger + Proactive Attention subsystem from scratch, then the user live-tested it on
their own restarted instance, finding one real prompt-quality bug this session's own
scratch testing hadn't.** See `handoff-archive.md` § "Heartbeat + Trigger + Proactive
Attention built from scratch, then live-tested by the user with two real fixes" for the
full build/testing narrative; root `CLAUDE.md`'s new "Heartbeat" section and
`server/heartbeat/CLAUDE.md` for the architecture. Jarvis can now notice things
independent of any open conversation (background Job status, time-sensitive Memory
commitments — day one; the mechanism itself is generic, more sources plug in later with
no rebuild) and, when a model judges it genuinely warranted, speak up first with no
message from the user — reusing Jobs' own Tier 1/2/3 Interruption Broker (generalized
via a real `db.js` migration, not duplicated) rather than a second alert system.
**Architectural path, plan approved before code.** Own scratch-server verification
caught and fixed one real bug before anything reached the user: a Tier 3 verdict never
creates an outbox row, so a routine, correctly-non-urgent job permission ask was
re-notifying (and re-spending a model call) every ~3 minutes forever — fixed with
per-source dedup memory. **The user's own live testing on their real restarted instance
then found what scratch testing hadn't**: quiet-hours-holds (Test 1) and genuine-urgency
speaks-up (Test 2) both passed live, screenshots confirmed; the emergency-breaks-through-
quiet-hours case (Test 3) failed once — the urgency model fixated on a job's own
"OK to start?" phrasing rather than the real stakes described — fixed by tightening
`decision.js`'s prompt, re-verified against the exact failing scenario 5 times (the
phrasing-fixation reasoning is gone), though the emergency threshold itself still isn't
perfectly consistent on a vaguely-worded scenario — disclosed as a real, accepted
limitation of judgment-by-model, not claimed as fully solved. **Test 3 has not yet been
re-run live by the user against the fix** — see "Waiting on the user" below. Nothing
committed.

**Most recent session before that (2026-09-02, later the same day, continuing directly from the
Self-Model build below): audited the Self-Model subsystem for which self-awareness
claims are genuinely code-backed versus prompt-only narration, then built two of four
planned remediation fixes — stopping for the user's confirmation after each, per their
own explicit instruction.** The audit (delivered in-conversation, not written to a
file) classified every self-modeling mechanism as Real/Prompted/Hybrid with file:line
citations, re-reading the actual code rather than trusting this project's own prior
session summaries — the audit's own explicit instruction. Real findings: the
reliability tallies (dimension 2) and the authority ceiling (dimension 6, enforced by
the import graph, not just described) hold up; dimension 5 ("how it knows") retrieves
real data but nothing checked whether the model's own sentence afterward actually used
it faithfully; the recorder itself (`self-capture.js`) had no local error handling, so
a broken sensor and a genuinely-unused tool were indistinguishable from every dimension
that depends on it.

**Fix 1 (sensor health)**: `self-capture.js`'s `recordAttempt()` call now runs in its
own local try/catch; either branch logs to a new `capture_health` table (`db.js`
migration 11) via `self-store.js`'s `recordCaptureHealth()`/`captureHealthSummary()`.
`check_myself`'s `can_do` dimension always includes `sensorHealth` now. Verified by
inducing a REAL SQLite failure (dropping the underlying table, not a mock) and
confirming it's caught, logged, and reported. **User-tested live on their own real,
restarted server** with three plain-language questions — the recorder was empirically
proven alive (`attempts24h` went 0→2 after one real, deliberate action), independent
confirmation beyond anything verified in this session alone.

**Fix 2 (utterance provenance)**: new `server/self/self-verify.js`; `db.js` migration
12 (`self_model_snapshots`, `self_model_citations`). Every `check_myself` call now
persists its exact result and logs every NUMERIC checkable fact in it as a citation
candidate; `verifyCitation(snapshotId, toolCallId, fieldName)` independently re-reads
the real snapshot and the real reply that followed it (never trusting its own logged
candidate value) and returns `used`/`ignored`/`unverifiable` — free text is explicitly
marked unverifiable, never faked as checkable, per the user's own explicit scoping
("don't try to verify free-form prose"). **A real bug was caught by the fix's own
verification, not shipped**: the first version's number matching used plain substring
containment, so a citable value of `0` falsely matched inside the text `"100%"` (since
`100%` contains the character `0`) — fixed with word-boundary-safe matching, re-verified
in both directions (a real citation is never missed; a coincidental digit inside a
longer number never falsely counts).

**A real mistake during Fix 2's own verification, disclosed immediately, not after the
fact**: a chained shell command lost its scratch-directory environment override on its
second half, which ran the two new migrations directly against the REAL
`data/jarvis.db` while the user's real server was live. Confirmed no harm — both
migrations are purely additive (new tables only, nothing existing touched), completed
cleanly, and the real server was confirmed still responding normally immediately after.
Functionally the same schema change that would have happened on the user's own next
restart anyway, just triggered by an accident instead of a controlled restart.

**Fix 4 (`whatsItsCall` prose drift) built and verified immediately after, in the same
session — the smallest of the four, no open design questions.** `self-model.js`'s
`memoryApprovalFloorText()`/`improvementEvidenceFloorText()` now build the `hardFloor`/
`hardFloors[0]` sentences FROM the live `autoSaveThreshold`/`minEvidence` values as their
own template argument, instead of hand-typed prose sitting next to (but never quoting)
the number it described. Regression-tested by mutating `memory-policy.js`'s
`THRESHOLDS`/`improvement-policy.js`'s `MIN_EVIDENCE_BY_TRUST` directly at runtime (both
are plain, unfrozen exported `const` objects — no mocking needed) and confirming the
prose picks up the new number, including the `Infinity` ("nothing auto-saves at all")
branch specifically, not just the finite-threshold wording.

**Fix 3 (goal-alignment check) — the one fix in this whole remediation that genuinely
couldn't be pure code — stopped at that fork, got the owner's real decision, then was
built on that basis.** Unlike Fixes 1/2/4, "does this declared goal actually serve what
the user asked" is a semantic judgment, not a fact lookup — no amount of string matching
proves or disproves it the way Fix 2's numeric citation check could. Three honest
options were laid out; **the owner chose the third: no pre-computed verdict at all — hand
the model the real goal text and the real original message side by side, and let it
judge freshly each time it checks in**, the same way dimension 6 already hands it real
policy numbers instead of a pre-baked answer. Built as: `track_goal.js` now snapshots
the real text of the user's own most recent message the instant a goal is declared
(`latestUserTurnText()`, reading `conversation.js`'s live window — never a second source
of truth); `db.js` migration 14 adds `self_goals.source_turn_text`; dimension 9's
`instruction` tells the model to compare both texts honestly and state whichever is true
— aligned, drifted, or too ambiguous to judge. Verified against three real cases through
the actual dispatch path: a goal declared with a real prior user message (the source
text captured correctly, the "judge for yourself" instruction present); a goal declared
into an empty conversation (`sourceTurnText: null`, and the instruction says plainly
there's nothing real to check against, rather than defaulting to "aligned"); and no
active goal at all. Docs and this entry updated in the same pass — nothing left stopped
mid-fix this time.

**Most recent session before that (2026-09-02, earlier the same day): built the Self-Model subsystem (`server/self/*.js`)
from scratch per the user's own nine-dimension spec, then — during the user's OWN live
testing of it — found and fixed two real, pre-existing bugs in the Skills confirm/token
system that predate this session's own work.** See root `CLAUDE.md`'s new "Self-Model"
section and `server/self/CLAUDE.md` for the architecture (a read-only assembler over
Memory/Jobs/Self-Improvement/Personality, zero model calls anywhere, an authority ceiling
enforced by the import graph rather than a prompt instruction — verified live, not just
read). Architectural path (brainstorming → clarifying questions → written plan →
approval), since it crosses four existing subsystems; the user confirmed all four
recommended defaults before any code. Verified via direct `capabilities.js`-dispatch-path
calls (goal declare/pull/close, a no-track-record refusal, a full grounding smoke test
across all nine dimensions) and a real scratch-server boot — never against a real model
call, to avoid touching the user's `.env`. **The two bugs, both found via the user's own
real test conversation, not this session's own testing:** (1) no confirm-gated tool (15+,
plus any folder Skill's own pipeline confirm) ever declared `confirm_token` as a real,
schema-visible argument — only as prose in `prompt.js`'s system instruction — so a
schema-strict model had no way to comply once told to resend it; fixed once, centrally,
in `capabilities.js`'s `getToolDeclarations()` (`withConfirmToken()`). (2) Fixing (1)
exposed a more serious, pre-existing gap underneath it: once `confirm_token` was real, a
model told "skip asking me" could mint the token and immediately redeem it in the SAME
turn, completing an entire ask-and-answer confirm round trip with zero real human reply
in between — confirmed live on a real free-tier model against real production data (read
the actual persisted `toolCalls`/`toolResults` payload from `messages`, not the user's
own paraphrase, to get the real sequence — now a permanent root `CLAUDE.md` verification
technique). Fixed per the user's own explicit choice (never let an explicit "skip
confirming" request bypass a real confirmation, full stop): `models/runner.js`'s
`runTurn()` now mints one `turnId` per call; `capabilities.js`'s `consumePendingToken()`
refuses a token redeemed in the same turn that minted it. **Both fixes verified via
direct, controlled reproduction only** (same-turn refused, cross-turn succeeds, a
no-turnId caller unaffected) — **neither has been re-tested by the user in a real live
conversation since restarting their server**, which is the one thing still needed before
calling this fully closed. **Since committed** — this entry originally said nothing was
committed yet; a later commit this same day (`5396bfd`, "Add Self-Improvement and
Self-Model subsystems") landed everything through this point, made outside this
session's own actions (most likely the user directly, or the concurrent session below).
Same branch (`jobs-subsystem-and-backlog`), and this is very likely the "concurrent
Self-Model session" the Self-Improvement Phase 7 entry below flagged as running in
parallel.

**Most recent session before that (2026-09-01, separate from the Self-Improvement work below):
investigated, then fixed, the voice turn-taking experience the user reported as
"unnatural."** Traced the full mic → VAD → STT → turn-detection → LLM → TTS → barge-in
pipeline across all three voice engines with file:line evidence BEFORE touching any code
(plan mode, user approved the plan) — full trace in
`plans/i-need-you-to-stateless-kettle.md`. **The real live root cause**: the user's
active `voiceEngine` setting is Full-duplex, but no Deepgram key is configured
(`data/external-services.json` has a `deepgram` label row with nothing behind it), so
`DuplexEngine` was silently running its free browser-STT fallback — the exact same
Chrome `SpeechRecognition` + static last-word-lookup countdown as the default Pipeline
engine, not Deepgram's real `UtteranceEnd` endpointing (fully built, currently dormant).
Fixed, scoped to that shared free-tier fallback per the user's own decision (fix the
free path now, structure it so a future Deepgram key is a drop-in upgrade): (1)
`public/turn-detector.js`'s new `SilenceWatcher` replaces "elapsed time since Chrome's
last transcript event" with genuinely-sampled continuous mic silence (same fixed-clock
RMS technique the barge-in sampler already uses) — `computeWaitMs()`'s lexical
wait-duration table is unchanged, it now gates against real measured silence instead of
wall-clock time since an event. (2) `DuplexEngine`'s fallback path gained the energy
re-check `PipelineEngine` already had — a real, previously-undetected asymmetry between
the two "same technology" fallback paths. (3) A one-time system note now tells the user
when Full-duplex has silently fallen back to browser STT, previously completely
invisible. **Deliberately left untouched**: the barge-in sustain-time asymmetry (250ms
Pipeline vs 450ms Duplex) — flagged in the plan as needing an explicit user decision,
not a silent pick, since 450ms was raised specifically after real false-trigger reports.
Verified: `node --check` clean on all 5 changed files
(`public/turn-detector.js`/`pipeline-engine.js`/`duplex-engine.js`/`voice-engine.js`/
`app.js`), plus an 11-assertion standalone logic test (no server/browser needed, per
CLAUDE.md's pure-logic-module testing guidance) proving `SilenceWatcher` fires only on
genuine sustained silence, never while actively speaking, and correctly restarts its
clock on re-arm. **Not verified**: how the new timing actually feels in a live
conversation, and that the new system note renders — both need the user's own manual
test in their regular browser, not something an automated session can confirm. Nothing
committed — same branch (`jobs-subsystem-and-backlog`).

**This session (2026-09-01) built the Self-Improvement subsystem from scratch, per the
user's own spec, across all 6 planned phases — Jarvis now reviews its own completed
work, extracts lessons, detects genuinely recurring patterns (never a one-off mistake),
applies small well-evidenced behaviour rules to itself automatically, and always asks
first for anything sourced outside its own experience or needing real code.** See
`server/improvement/CLAUDE.md` for the module breakdown and root `CLAUDE.md`'s new
"Self-Improvement" section for the design decisions; the session log entry below for the
build/verification narrative. Every phase was verified before the next started — real
SQLite migration + truth tables (Phase 1), the actual jobs event bus including the
confirmed-live double-emit and split-completion edge cases (Phase 2), a real stub model
proving the `runner.js`/`prompt.js` cache-correct threading end to end (Phase 3), a
budget-ledger stress test proving exactly 2 model calls across 10 compressed ticks, not
10 (Phase 4), a full `agent-browser` pass including the undo refuse-vs-clobber confirm
dialog and confirming the auto-apply notification lands in the bell and never the
transcript (Phase 5), and a REAL live web-research call (no stub) confirming the
corroboration floor and the domain-exclusion guard against a genuinely relationship-
flavoured memory (Phase 6). **Two real design gaps were found and fixed mid-build,
before anything shipped** — worth knowing if this area is touched again: (1)
`synthesize.js` was hardcoding a rule proposal's `sourceTier` to 1 regardless of its
supporting lessons' real tiers, which would have let an outside-research-derived pattern
slip past the "outside ideas always ask" floor by riding along with genuinely tier-1
evidence — fixed to take the WORST tier among supporting lessons. (2) `improvement_outcomes`
needed a second id column (`entity_ref`, distinct from the per-event `source_ref`) once
it became clear a recurring scheduled task's own runs each get a fresh run id, with no
stable key otherwise for `reflect.js`/`synthesize.js` to group "this specific task keeps
failing this way" under. **Nothing from this session is committed** — same branch
(`jobs-subsystem-and-backlog`), stacking on whatever the branch already held.

**Same session, Phase 7 (immediately after): the user's own first real test pass on
their live instance found two real bugs, which are now fixed, plus a detail/edit/
archive-delete UI extension the user asked for.** (1) `record_lesson`/`suggest_improvement`
were declared as real tools but `SYSTEM_INSTRUCTION` never told the model WHEN to
actually call them — the model answered in prose and filed nothing, exactly what the
user saw live. Fixed with an explicit trigger paragraph mirroring Memory's own
(`prompt.js`, right after the Memory section); proven fixed with a real stub-model
tool-call capture, not just reading the prompt text. (2) A genuinely latent bug, only
surfaced by re-running the Phase 1-6 regression scripts as part of this pass (Phase 2's
own script had never been rerun since Phase 4 added reflect/synthesize into the same
tick) — `reflect.js` marked every reviewed outcome's batch reviewed EVEN WHEN the model
call itself failed (no model available), which on this user's own real, routinely-all-
rate-limited model roster (see CLAUDE.md's Gotchas) would have permanently discarded
real learning material on nothing more than "quota was tight this exact tick." Fixed:
only a genuine, successful model response marks outcomes reviewed now. The UI extension
(click any Rule/Lesson/Suggestion card for detail; Rules are editable; Archive/Restore/
Delete-permanently for all three, reusing Memory's own proven pattern rather than a new
"Recycle Bin" concept, since a Rule's own Undo button depends on the row surviving) — a
NEW migration 8 (not an edit to migration 7, which had by then genuinely run against the
user's real database), `agent-browser`-verified end to end including a real bug caught
live during that pass (clicking Archive/Restore/Delete inside a detail modal never
called the modal's own `api.close()`, leaving its scrim silently blocking the whole page
underneath — fixed, re-verified). **A concurrent session was actively building an
unrelated "Self-Model" subsystem (`server/self/*.js`) in this same repo throughout this
pass** — confirmed via live file-change notices (new imports appearing in `runner.js`/
`prompt.js`, a new migration 9 in `db.js`); no actual collision with this session's own
files, but worth knowing before assuming this branch's state matches what's described
here alone.

---

**Earlier the same day, a separate session ran a full audit + remediation, then fixed
two real, live-verified bugs the user reported after using the app.** See
`handoff-archive.md` § "Full audit + remediation, skeptical re-verification, TTS/voice
provider debugging, model fallback/health architecture fix, connector picker redesign"
for the full five-phase narrative; the session log entry below for the short version.
Headline outcomes:
- **Model fallback/health architecture fixed at the root**, not patched superficially,
  per explicit instruction to check the architecture first. Two causes, both verified
  directly against the user's real `data/models.json` (not assumed): a quota error was
  being misclassified as a 6-hour "unreachable" ban instead of a 30-minute "quota" one
  (`server.js`'s `testAndRecord()` was classifying already-friendlied text instead of
  the raw detail); and `router.js`'s ranking had zero awareness of whether a model was
  actually known to work, letting a dead-but-high-tier model outrank the only real
  working ones. Both fixed and verified against real + scratch-simulated data — root
  `CLAUDE.md`'s Model system section still needs updating with these facts (folded
  into Next steps #8 below, not yet done).
- **The connector picker (Schedule/Task + Morning Briefing) was built, then redesigned
  twice on direct user feedback** — real toggle switches instead of checkboxes, logos
  always on, a capped inline list with a "View all" overlay for the rest, and the
  "Connect another app in App Control" shortcut removed from Briefing. A real
  architectural conflict (`_modal.js` cancels whichever modal is already open the
  moment a second one appears — this picker lives inside the Task screen's own modal)
  was found and designed around, not discovered by breaking something live.
- **TTS/voice provider debugging** root-caused ElevenLabs's silence to real account
  quota exhaustion, misclassified into a generic "no API key" message (diagnosed, not
  fixed — deprioritized by the user); built the "any provider, zero extra config"
  system the user had been asking for (`server/tts/generic.js`, Fish Audio as the first
  real entry), after one real process mistake (built unrequested UI without describing
  the plan first, caught by the user, fully reverted) corrected mid-session.

**Everything accumulated since the last commit (`bfae8e5`) — this session's own work,
plus the previous session's first-class Skills architecture and personality/reaction-
sounds work, all still on `jobs-subsystem-and-backlog` — was committed at the user's
explicit request once this session's work was confirmed working: `9ebb201`.**

**Still open, not investigated further this session**: a background security review
flagged two real SSRF findings — `server/server.js` and `server/tts/generic.js`'s
custom-TTS-endpoint route accept a user-supplied URL with no host/protocol validation
before the server fetches it. Not fixed, not asked for; worth a look next time
security-adjacent work is in scope. **Also still open**: ElevenLabs's quota-error
misclassification (see above); the real laugh sound is still a placeholder
(`public/sounds/laugh.wav`, blocked on ElevenLabs account quota); the `[[laugh]]`
marker's placement/frequency instructions aren't reliably followed by weaker/free-tier
models.

## Next steps

1. **`jobs-subsystem-and-backlog` now holds everything through this session's own work,
   committed, but still not merged to `main` and not pushed.** Nothing blocks either —
   merging/pushing weren't asked for, only committing was; worth confirming explicitly
   rather than assumed next time it comes up. Either way, the user's live server needs
   an ordinary restart to pick up ANY of this — none of it is live yet (same "don't
   restart their instance yourself" caution as always).
2. **Whether to build an adaptable `generic` worker is still an open design
   question**, raised by the user right after Jobs shipped — see CLAUDE.md's Jobs
   section. Not a bug, not blocking anything; worth surfacing early to whoever picks
   this up next since the user was actively mid-thought on it.
3. **RESOLVED for a plain (`SKILL.md`-only) Skill, confirmed live by the user — but a
   `skill.toml` Pipeline Skill specifically was never separately exercised.** The
   `core:true`/`unlocked` premise this item was originally worried about was already
   wrong by the time of the 2026-08-31 → 2026-09-01 session (folder Skills, Pipeline
   ones included, are `core: true` — `find_capability` was never actually needed for
   discovery); what that session found and fixed instead was that nothing told the
   model to *prefer* a matching Skill, and that Jobs/briefing couldn't reach one at all
   structurally. Both are now fixed and confirmed live for a `SKILL.md`-only test
   Skill. A `skill.toml`-driven Pipeline Skill uses the identical declaration path
   (`folderSkillToTool()`), so the same fix should generalize, but this hasn't been
   separately watched happen — worth a quick real test if a Pipeline Skill is in play.
4. **Action the remaining CLAUDE.md staleness findings** — full list in
   `plans/audit-handoff-md-in-this-graceful-thompson.md`. Two of the highest-value
   items (the connector Client ID/Secret UI design, and `client-identity.js`/CIMD) are
   now resolved as part of this session's docs pass — `server/connectors/CLAUDE.md`
   documents both the final "no UI anywhere" state and CIMD/`catalog-credentials.js` in
   full. Still open: `friendly-message.js` and the icon system are undocumented; the
   connector permission model is documented as 2-state but is 4; `CONTROL_TOOLS` is
   documented as flat tools but is 3 tools plus a batched `actions[]` array.
5. **The WSL sandbox backend has still never run against a real WSL distro**
   — none is installed on this dev machine. `server/sandbox/wsl-backend.js`
   is reasoned through carefully but unverified; test for real the first time
   WSL exists here or on another dev machine.
6. **Task A is partially done now, not fully.** The 2026-08-31 → 2026-09-01 session
   confirmed the single-skill-trigger case with real logs/traces (a scripted stub
   model's exact request/response captured, not Jarvis's own claim) AND, separately, a
   live confirmation from the user on their real restarted instance. Still not
   exercised: correct pick among SEVERAL installed Skills, a genuinely ambiguous case,
   and a no-match negative case (does it correctly do nothing when no Skill fits,
   rather than over-eagerly reaching for one on a stretch?). Worth a real conversational
   pass once there are enough installed Skills to make "correct pick among several" a
   meaningful test — right now there are only 4.
7. **No AI-driven control session has ever reached `report_done` unbroken**
   end to end (open → act → save → verify → done) — every primitive is
   verified individually; the full happy path with the newer close/restore/
   arrange actions and scratch-window auto-cleanup has not been watched to
   completion.
8. **Root `CLAUDE.md`'s Model system section still doesn't mention**
   `server/models/task-types.js` (task-aware routing), the `model_health` SSE
   event, or the `addModel()`/`discoverModels()` `(connectionId, model)`
   duplicate-prevention rule — unrelated to this session's own Model system
   addition (the `allowedTools` enforcement note). A small, bounded
   double-render around "Check all models" (SSE-triggered re-render racing
   the button's own) was also flagged as a known, low-priority cosmetic item,
   deliberately left unfixed.
9. **The `[[laugh]]` marker's placement/frequency instructions aren't reliably
    followed by weaker/free-tier models** (see CLAUDE.md's "Real Vocal Laughter"
    section) — a real production test caught a violation of both rules in one reply.
    Left as-is at the user's explicit request not to keep touching code mid-session;
    worth tightening if it recurs once retested against a model that isn't
    Auto-selected free-tier fallback.
10. **Two real SSRF findings from a background security review, never investigated**:
    `server/server.js` and `server/tts/generic.js`'s custom-TTS-endpoint route fetch a
    user-supplied URL with no protocol/host validation first (could be pointed at an
    internal address). Unrelated to any specific session's own work — surfaced
    mid-session, flagged to the user, not acted on.
11. **ElevenLabs's quota-exhaustion error is still misclassified as a generic "no API
    key" message** (`server/tts/elevenlabs.js`'s 401-status mapping, then further
    collapsed by `server.js`'s route) — root-caused and confirmed live (2026-08-31 →
    2026-09-01 session), not fixed since the user deprioritized it in favor of the
    Fish Audio "any provider" work. Worth a real fix (classify on the actual response
    body, not just the HTTP status) next time voice-provider work is in scope.
12. **The voice turn-taking fix (`SilenceWatcher`) needs a live browser test** — logic
    verified standalone (11 assertions), but how the new real-silence-based countdown
    actually feels in an actual conversation, and whether the new "Full-duplex fell
    back to browser STT" system note renders correctly, is unverified. Also still
    open, deliberately deferred: whether to unify the barge-in sustain-time asymmetry
    (250ms Pipeline vs 450ms Duplex) — see "Right now" above and
    `plans/i-need-you-to-stateless-kettle.md`'s tradeoffs section.
13. **The turn-boundary confirm fix (`consumePendingToken()`'s same-turn refusal) needs
    a real live retest** — verified only via direct, controlled `invoke()` calls (this
    session's own diagnostic), never through an actual model/turn loop with a real
    model. Retest the exact scenario: "Remember that I'm allergic to peanuts" then
    "Forget that, don't ask me to confirm" — it should now genuinely pause for a
    separate reply, no matter how the second message is worded.
14. **RESOLVED — all four Self-Model audit fixes are built, verified, AND now
    confirmed live by the owner's own real testing; nothing outstanding from that
    remediation.** Fix 1 (sensor health) was confirmed live earlier the same day
    (`attempts24h` moved 0->2 on the owner's own real server). Fix 3 (goal-alignment)
    was confirmed live after that, on a real, deliberately staged drift scenario — the
    owner planned a birthday party, then pivoted to asking about a laptop, then asked
    Jarvis to be honest about whether it had drifted. Checked against the raw
    persisted tool-call record, not just the reply text: `track_goal` genuinely fired
    on the real opening message, captured the exact source text verbatim (down to the
    literal line break in how it was pasted), and the later `check_myself` call
    returned that real data with `grounded:true` — the model's own answer ("yes, we
    have drifted") was a faithful reflection of that real record, and the objectively
    correct verdict. **One real, disclosed gap found along the way, not a code bug**:
    a first attempt at this same test failed to exercise the mechanism at all, because
    a single opening question wasn't enough to make the model actually call
    `track_goal` — it took a more detailed, explicit follow-up message before the goal
    was recorded. A real model-reliability gap on the current lineup, the same class
    already documented elsewhere in this file, not a flaw in what was built. Fixes 2/4
    remain verified only via direct dispatch-path calls, not a live model conversation
    — lower-risk to leave that way (2 is forensic/backend-only, 4 has no user-facing
    surface at all), but worth knowing if this area comes up again.

## Waiting on the user

- **A live retest of Heartbeat's Test 3 (emergency breaks through quiet hours)**, after
  restarting the real server — the fix (tightening `decision.js`'s prompt so it judges
  real stated stakes rather than a job's own polite "OK to start?" phrasing) was
  re-verified 5x against the exact failing scenario in a scratch test, but not yet
  against the user's own live instance. Test 1 (stays quiet) and Test 2 (genuine urgency
  speaks up) are both already confirmed live — only Test 3 remains. Worth trying with a
  sharply-worded scenario (a hard dollar figure and a hard deadline) — the emergency
  threshold itself was found to still be somewhat inconsistent on vaguely-worded cases,
  a disclosed, accepted limitation of judgment-by-model, not something the prompt fix
  fully eliminated.
- **A live retest of the confirm-gate turn-boundary fix**, after restarting the real
  server — "Remember that I'm allergic to peanuts" then "Forget that, don't ask me to
  confirm" should now genuinely require a separate reply, not complete in one breath.
  Verified only by direct simulation so far, never against a real model turn.
- **A live browser test of the voice turn-taking fix** (new `SilenceWatcher` in
  `turn-detector.js`, plus the "Full-duplex fell back to browser STT" system note) —
  standalone logic is verified, but how it actually feels, and whether adding a real
  Deepgram key (recommended in the investigation) is worth the ongoing cost, are both
  the user's own call once they've tried it.
- **A merge/push decision for `jobs-subsystem-and-backlog`, then an ordinary restart of
  the user's own live Jarvis** — the branch now holds EVERYTHING committed to date
  (Chat Persistence + Memory, the tools/skills split, Pipeline Skills, the duplex voice
  rebuild, connector fixes, Background Task Orchestration, first-class Skills,
  personality/reaction-sounds, and this session's audit/TTS/model-fallback/connector-
  picker work), none of which is live on the currently-running instance, which predates
  all of it. Not merged, pushed, or restarted automatically — the user was actively
  using the instance and none of those are decisions to make without asking. Once
  merged and restarted, the specific test steps already given in-chat for each piece
  are what to run through.
- **A real interactive OAuth login click-through** — now genuinely done for Notion,
  Composio, and (this session) Gmail-through-Composio, all real accounts, all
  confirmed Active. Still outstanding, and — new fact worth recording — can no longer
  even be attempted from the UI at all after this session's "no Client ID/Secret UI
  anywhere" decision: GitHub, Slack, Google Drive, Gmail-direct (Jarvis's own catalog
  entry, distinct from the Gmail-via-Composio connection that IS live). The only
  remaining path for any of these is `catalog-credentials.js`'s `register-client`
  route, called directly with a real Client ID/Secret — never through a screen. Slack
  specifically still carries its own separate, unresolved risk even with a Client ID:
  its docs require an HTTPS redirect URI and Jarvis's is plain HTTP.
- **The user's own "Claude itself rejects a plain zip/folder" claim** was
  never resolved — screenshots of that specific error were requested twice
  and never arrived. Everything else diagnosed in that session turned out to
  be genuine Jarvis-side bugs unrelated to this specific claim.
- **A real laugh sound clip, to replace the current synthesized placeholder
  (`public/sounds/laugh.wav`)** — ElevenLabs generation of one is blocked on account
  quota (3 credits remaining, 46 required). Either wait for quota to refresh, add
  credits, or the user supplies a real recorded clip directly; whichever path, it's a
  one-file swap in `public/reaction-sounds.js` once sourced, nothing else changes.

## Not yet written up

*(empty — the three sessions that had accumulated here as of this restructure
are now in the session log below, marked as reconstructed.)*

## Session log (newest first)

### 2026-09-02 — Heartbeat + Trigger + Proactive Attention built, then live-tested with two real fixes
See "Right now" above for the full account; `handoff-archive.md` § "Heartbeat + Trigger
+ Proactive Attention built from scratch, then live-tested by the user with two real
fixes" for the full build/testing narrative; root `CLAUDE.md`'s new "Heartbeat" section
and `server/heartbeat/CLAUDE.md` for the architecture. Built the user's own spec for
proactive noticing/speaking with no hardcoded watch-list or emergency-category list
(architectural path, plan approved). Generalized Jobs' existing Interruption Broker
(`db.js` migration 13) rather than building a second one — every existing Jobs call site
needed zero changes. Own scratch testing (a real copy of the user's database, a scratch
server, `agent-browser`) caught and fixed a real dedup bug (Tier 3 findings re-notifying
forever) before it ever reached the user. The user's own live testing then found a real
prompt-quality bug scratch testing hadn't (the urgency model fixating on a job's own
polite phrasing over its actual stated stakes) — fixed and re-verified 5x against the
exact failing case; the emergency threshold itself is still not perfectly consistent on
a vague scenario, disclosed as a real limitation. Test 1 and Test 2 confirmed live by
the user; Test 3's fix not yet re-tested live. Nothing committed.

### 2026-09-02 — Self-Model audit (Real/Prompted/Hybrid, file-cited), then all four remediation fixes built and verified
See "Right now" above for the full account. Audited every self-modeling mechanism in
the codebase for the owner (Real/Prompted/Hybrid, re-reading actual code rather than
trusting prior summaries) — the reliability tallies and the import-graph-enforced
authority ceiling held up as genuinely real; the recorder's own lack of error handling
and dimension 5's unchecked "did the reply actually use the data" gap were the two
highest-priority findings. Built Fix 1 (`capture_health` table, wrapped
`recordAttempt()`, `sensorHealth` in `check_myself`) — verified against an induced REAL
SQLite failure, then confirmed alive on the user's own real server via three live test
questions. Built Fix 2 (`self_model_snapshots`/`self_model_citations`,
`server/self/self-verify.js`'s `verifyCitation()`) — a real substring-matching false
positive (`0` matching inside `"100%"`) was caught by the fix's own test before
shipping, not after. One real mistake mid-session, disclosed immediately: a chained
shell command accidentally ran both new migrations against the real database while the
server was live — confirmed harmless (additive schema only, server unaffected) but a
genuine testing-discipline slip. **Immediately after, in the same session**: built and
verified Fix 4 (`whatsItsCall`'s hardcoded prose replaced with sentences built from the
live threshold values themselves; regression-tested by mutating the real threshold
objects at runtime, no mocking needed). Fix 3 (goal-alignment check) stopped at a real
fork instead of picking silently — unlike the other three, it needs a genuine semantic
judgment no pure-code heuristic can honestly make — laid out three honest options for
the owner, got their answer (no pre-computed verdict; hand the model both real texts and
let it judge live), and built it on that basis: `track_goal.js` now snapshots the real
user-turn text a goal is declared from, and dimension 9 hands both texts to the model
with an instruction to judge freshly rather than assume alignment. Verified against
three real cases through the actual dispatch path (a real source turn captured
correctly; an empty conversation honestly returning a null source with no fabricated
"aligned" default; no active goal at all). All four fixes from the audit are now
complete.

### 2026-09-02 — Self-Model subsystem built, then two real pre-existing confirm/token bugs found and fixed via the user's own live testing
See "Right now" above for the full account; root `CLAUDE.md`'s new "Self-Model" section
and `server/self/CLAUDE.md` for the architecture. Built the user's own nine-dimension
self-knowledge spec (architectural path, plan approved) — a grounded, read-only assembler
with zero model calls and an authority ceiling enforced by the import graph, not a prompt
instruction. The user's own real test conversation then surfaced two bugs in the
pre-existing Skills confirm system, neither part of this build: `confirm_token` was never
a real, declared tool-schema argument (only prose), and — once that was fixed — a model
could complete an entire confirm round trip in one turn with no real human reply, when
explicitly told to skip asking. Both fixed (a central schema-injection function in
`capabilities.js`; a per-turn id now enforced by `consumePendingToken()`), diagnosed by
reading the real persisted tool-call payloads rather than trusting a paraphrased
transcript — the technique that resolved which of two very different explanations was
actually true. Verified via direct dispatch-path calls only; a real live retest by the
user, after restarting their server, is still needed.

### 2026-09-01 — Voice turn-taking investigated (full pipeline trace), then fixed at the actual live root cause
See "Right now" above for the full account; `plans/i-need-you-to-stateless-kettle.md`
for the complete file:line-cited investigation (mic → VAD → STT → turn-detection → LLM
→ TTS → barge-in, all three engines). User reported the voice conversation "feels
unnatural." Traced everything before writing any code (plan mode, approved by the
user), which surfaced the real cause: Full-duplex is selected but has no Deepgram key,
so it silently runs the same free Chrome-STT + static-timer fallback as the default
Pipeline engine. Fixed the shared fallback's turn-end detection (new `SilenceWatcher`
in `turn-detector.js` — real continuous-silence sampling instead of time-since-last-
ASR-event), closed a Pipeline/Duplex asymmetry (the fallback path was missing the
energy re-check Pipeline already had), and added a one-time visibility notice for the
silent fallback. Barge-in sustain-time asymmetry (250ms/450ms) deliberately left alone,
flagged as needing an explicit decision. `node --check` plus an 11-assertion standalone
logic test pass; live-in-browser feel is unverified — needs the user's own test.

### 2026-09-01 — Self-Improvement Phase 7: two real bugs from the user's own first test, plus detail/edit/archive UI
See "Right now" above for the full account. Short version: the user tested the freshly-
built subsystem live and reported two things that looked broken (teaching Jarvis
something, and asking it to suggest an improvement, both replied normally but filed
nothing) plus asked for cards to be clickable, editable, and deletable-with-a-safety-net.
Root-caused bug 1 to a missing system-prompt trigger paragraph (the tools existed but
nothing told the model to actually call them — fixed, proven with a real captured
tool-call, not just re-reading the prompt). Found bug 2 independently, by actually
re-running the OLD Phase 1-6 regression scripts rather than assuming they'd still pass —
`reflect.js` was silently discarding real outcomes on a failed model call, which matters
a great deal on this user's own routinely-rate-limited model roster. Built click-to-
detail + rule editing + Archive/Restore/Delete-permanently for Rules/Lessons/Suggestions
(Memory's own proven pattern, not a new "Recycle Bin"), migration 8, new store functions,
new routes, a rewritten screen — caught and fixed one more real bug live during the
`agent-browser` verification pass (a detail modal's own terminal actions never closed
the dialog). Discovered mid-session that a concurrent session was building an unrelated
"Self-Model" subsystem in the same repo the whole time — no actual file conflict with
this session's own work, confirmed by re-checking before every edit, but the branch now
holds both. Nothing committed.

### 2026-09-01 — Self-Improvement subsystem built, all 6 phases, verified end-to-end
See root `CLAUDE.md`'s new "Self-Improvement" section and `server/improvement/CLAUDE.md`
for the architecture.

Full brainstorming → design → approval flow (architectural path, per the user's own
explicit spec covering learning from outcomes, pattern detection across multiple tasks
not one-off patching, noticing life patterns while excluding emotional state/
relationships entirely, source-trust tiering, auto-apply limited to rules+settings from
Jarvis's own history only, and a screenless-no-longer undo log). A Plan-agent stress
test caught six real design flaws before any code was written — the `runner.js:272`/
`prompt.js:277` threading actually needed real edits (verified directly, not assumed),
the orchestrator hook needed to target `job-events.js` not `orchestrator.js`, undo
needed to store `after` and refuse on mismatch rather than just storing `before`, a
`request_job_split` completion needed explicit exclusion from capture, a real budget
ledger was needed or a 15-minute tick could run away, and a minimum-evidence floor was
needed beneath even `auto` trust. All six were designed in before Phase 1 started.

Built and verified in the plan's own 6 phases, each against a scratch `JARVIS_DATA_DIR`/
port, real `node --check` sweeps, and the manual-verification techniques CLAUDE.md
itself prescribes (a `node:http` stub model for quota-free pipeline testing, `agent-
browser` for the screen). Two real bugs found and fixed mid-build before anything
shipped (see "Right now" above for both — the `sourceTier` hardcoding gap and the
missing `entity_ref` column), plus one real UI bug caught by the browser test itself (a
Changes-tab row showing a raw rule id instead of its actual text — fixed and re-
verified). Phase 5's browser pass covered the full approve → apply → pref-actually-
changed → undo → pref-actually-reverted round trip, and separately the refuse-vs-
clobber path (externally muting a rule after it was applied, then confirming the "this
changed since — restore anyway?" dialog actually appears rather than silently
overwriting). Phase 6 used a real, live, un-stubbed web-research call (DuckDuckGo →
dev.to, no API key needed) rather than a canned response, which also exercised the
corroboration-floor and tier-classification logic against genuine variance rather than
a fixture. Nothing committed this session.

### 2026-08-31 → 2026-09-01 — Full audit + remediation, skeptical re-verification, TTS/voice provider debugging, model fallback/health architecture fix, connector picker redesign
See `handoff-archive.md` § "Full audit + remediation, skeptical re-verification,
TTS/voice provider debugging, model fallback/health architecture fix, connector picker
redesign" for the full five-phase narrative.

Started via `council` with an explicit investigate-first/wait-for-approval rule, then a
second, stricter round with an explicit no-unverified-claims rule. Approved remediation
across tool-visibility, connector-awareness, and desktop-control hardening. Then a long,
corrective TTS/voice-provider debugging arc: root-caused ElevenLabs's silence to real
account quota exhaustion (misclassified into a generic message, not fixed), reverted a
real process mistake (built unrequested UI without describing the plan first, caught by
the user), and shipped the "any provider, zero extra config" system the user had been
asking for (`server/tts/generic.js`, Fish Audio first). Then, on a fresh report, fixed
the model fallback/health architecture at the root — two independently-verified causes
(a quota-vs-unreachable misclassification in `server.js`, and `router.js`'s ranking
having zero awareness of known-working/known-bad state) — verified against the user's
real model data plus a scratch-data worst-case simulation, not just read through. Then
built, and twice redesigned on direct screenshot feedback, a shared connector picker for
Schedule/Task and Morning Briefing — real toggle switches, always-on logos, a capped
list with a "View all" overlay, and a found-and-designed-around conflict with the app's
single-modal-at-a-time component. Everything accumulated since the last commit
(`bfae8e5`) — this session's own work plus the previous session's first-class Skills and
personality/reaction-sounds work — was committed at the user's explicit request as
`9ebb201`.

### 2026-08-31 → 2026-09-01 — First-class Skills architecture built (live chat + Jobs + briefing), verified end-to-end, confirmed by the user live
See `handoff-archive.md` § "First-class Skills architecture built (live chat + Jobs +
briefing), verified end-to-end, confirmed by the user live" for the full narrative;
root `CLAUDE.md`'s "Skills" section, `server/jobs/CLAUDE.md`, and
`server/scheduler/CLAUDE.md` for the architecture; `docs/superpowers/specs/2026-08-31-
first-class-skills-design.md` for the design.

Started from the user noticing Jarvis's own answer to "how many skills do you have"
was vague and folded in connectors, unlike a comparable answer from Claude Code itself.
Found the real gap by reading the code, not guessing: the data model already separates
Skill/Tool/Connector correctly, but nothing told a model to prefer a matching Skill,
and `research`/`files`-kind Jobs plus connector-restricted briefings structurally could
not reach a Skill at all. Fixed with small, targeted extensions to code already there
(a guidance sentence in `prompt.js`'s existing `skillsSection()`, a one-line filter
widening in `jobs/orchestrator.js`, a briefing.js change mirroring `scheduler.js`'s own
existing pattern) rather than a new mechanism. Verified against a real scratch server +
stub model — a real chat turn's full declare→invoke→instructions-round-trip, a real
Job's tool list, a real briefing preview — with three self-inflicted test-harness bugs
found and fixed along the way (documented in the archive entry so they aren't
rediscovered). Then handed the user plain-language manual test steps; **the user
restarted their real instance and confirmed live** that both "how many skills" and "how
many connectors" now answer accurately and separately. Nothing committed.

### 2026-08-31 — Adaptive Communication Register (personality) built + tested extensively live, then Real Vocal Laughter (reaction sounds) added
See `handoff-archive.md` § "Adaptive Communication Register (personality) built +
tested extensively live, then Real Vocal Laughter (reaction sounds) added" for the
full session narrative; root `CLAUDE.md`'s "Adaptive Communication Register" and "Real
Vocal Laughter" sections for the architecture.

Built the user's own personality spec (five dimensions, two hard rules, hybrid
code-floor/model-inference design), then a long live-testing loop finding and fixing
several real gaps only real phrasing surfaced (two regex misses, a repeated-check-in
bug, a reopened-settled-topic bug, a US-specific crisis-number gap). Then, on the
user's own follow-up ask, built a second, separate mechanism — real audible laughter
spliced into playback, never TTS reading "haha" as words — which required restructuring
`browser-speaker.js` from scratch and two real chunk-boundary bugs found only by
testing. Verified via live browser testing (isolating a browser-automation quirk from a
real bug) and a read-only diagnostic against the user's own real, currently-working
model, which also surfaced that nearly the user's entire model roster is
quota-exhausted, explaining live-testing flakiness. The real laugh sound itself is
still a placeholder — ElevenLabs generation is blocked on account quota. Nothing
committed this session.

### 2026-08-26 — Background Task Orchestration ("Jobs") built, 5 phases, then committed
See `handoff-archive.md` § "Background Task Orchestration ("Jobs") built, 5 phases, then
committed" for the full build narrative and every real bug found live; root
`CLAUDE.md`'s "Background Task Orchestration (Jobs)" section and
`server/jobs/CLAUDE.md` for the architecture.

Built the user's own background-task-orchestration spec in 5 verified phases (store +
policy, worker + supervisor, conversational tools + escalation, worker kinds + UI,
splitting), each caught at least one real bug only live testing surfaced — a capacity
gap, a circular-import mistake, a silently-inert flag, an unenforced tool allowlist, a
duplicate-escalation bug. Confirmed live, not assumed: crash-recovery classification
from the write-ahead trace, resume-with-real-context across a real `SIGKILL`, the split
depth ceiling holding under a directly-planted nested case, and a `computer`-kind job
never once auto-starting. Then committed everything on disk — this session's work and
every previously-uncommitted prior session's — for the first time, on a new branch. Left
open, at the user's own prompting: whether `kind` should stay a fixed tool-list-only
enum or become a genuinely adaptable, Orchestrator-configured worker.

### 2026-08-23 → 2026-08-24 — Voice/conversation rebuild: duplex engine, generic TTS, state-machine audit, four follow-up rounds
See `handoff-archive.md` § "Voice/conversation rebuild: duplex engine, generic TTS,
state-machine audit, four follow-up rounds"; full blow-by-blow in
`plans/i-would-like-you-snoopy-bengio.md` (still being added to).

Full-duplex voice built alongside the existing engines (Deepgram STT, a generic
provider-swappable TTS seam, real ElevenLabs backend), a situational-awareness system
prompt, model-router tie-break + tool-declaration-slimming fixes, a real state machine +
audio-reactive orb, and two composer-dictation bugs — all live-confirmed working. Then a
20-issue state-machine audit (generation tokens + ported per-sentence watchdogs closed
most of it), a generalized external-service key store, and four follow-up rounds
chasing real reported bugs: a typo'd service ref needing fuzzy (Levenshtein) matching
instead of a substring check; ElevenLabs cutting off mid-reply, root-caused to a
thinking-phase watchdog getting re-armed by events that also fire legitimately while
already speaking; a one-key-per-service collision; and, most recently, "stuck on
speaking" with Windows voice (closed with a new engine-level speaking watchdog) plus
finding the earlier cutoff fix had missed the `'restart'` branch. Nothing committed;
real playback still needs the user's own restart + retest, same limitation as every
voice fix in this whole arc.

### 2026-08-23 — Pipeline Skills (skill.toml) + tools/skills architectural split
See `handoff-archive.md` § "Pipeline Skills (skill.toml) + tools/skills architectural split".

The user asked for `skill.toml` (a fixed, ordered, auto-run pipeline as an alternative
or addition to `SKILL.md`) and separately questioned whether building real executable
capabilities (`get_weather`, `open_app`, ...) as "skills" had been a mistake, given
other systems keep Tools and Skills genuinely separate — asked for an honest
architectural assessment BEFORE any code, not a defense of what existed. Verified
before answering rather than from memory: confirmed the leak-prevention machinery
(`kind` discriminator, `listUserSkills()` as the only source a Skills UI may read) was
already real and working, but the *vocabulary* — "skill" meaning both the union and one
member of it (`runSkill`, `hasSkill`, built-ins living in `server/skills/`) — was what
kept regenerating the native-ability-as-Skill confusion the project's history already
documents three times. Found two things nobody had flagged: `GET /api/skills` (the
mixed list) had zero front-end consumers (dead code), and the task-action picker's
`type:'skill'` was a real, working server capability with no UI path to it (the
briefing picker that once exposed it was deleted outright for the native-ability-leak
reason, per this project's own history). Surfaced two real blockers the user hadn't
named: the circular-import invariant (a pipeline runner needs to call back into the
tool dispatcher, which would deadlock the dynamic-import loader without careful
injection) and the standing "Jarvis never executes downloaded code" rule (an uploaded
`skill.toml` genuinely is downloaded code that runs, unlike an uploaded `SKILL.md`).
User chose, via explicit fork questions: the full `server/tools/` split done first and
verified before any pipeline code (not just a lighter seam), templated step-to-step
data flow (not just end-of-run collection), the hand-rolled TOML subset as asked, and a
per-skill upload-approval gate mirroring `run_skill_script.js`'s existing
`scriptsApproved` shape. Plan approved, then executed and verified in 5 phases — see
`plans/can-you-give-me-async-crayon.md` for the full phase-by-phase record; summary:

Phase 1 moved all 37 built-in capability files to `server/tools/`, split the old merged
`server/skills/index.js` into a pure loader (`tools/index.js`) and a new composition
seam (`server/capabilities.js`, owning `invoke()` and the confirm-token gate moved
verbatim, plus the new `listStepCandidates()` a pipeline step-picker needs), and
shrunk `server/skills/index.js` to folder-Skills-only — caught and fixed a real bug the
move itself would have introduced (`control/session.js`'s direct `open_app.js` import)
before it ever shipped, and swept every stray doc comment across the whole server tree
plus 5 `CLAUDE.md` files (root, new `server/tools/CLAUDE.md`, rewritten
`server/skills/CLAUDE.md`, `connectors`/`control`/`scheduler`) for accuracy. Verified via
direct `invoke()` calls (built-in, folder Skill, unknown name, the confirm-token
round-trip including the specific mismatched-resent-args edge case this project's
history already documents as a real prior bug, `autoConfirm` bypass, the injected
`ctx.reservedSkillNames`) plus a live `agent-browser` check confirming zero built-ins
ever appear on the Skills screen. Phase 2 built a hand-rolled TOML subset
(`server/skills/store/skill-toml.js`: `[[inputs]]`/`[[steps]]`/`[steps.args]` only —
inline tables, dotted keys, dates, hex/octal all named parse errors, the opposite of
`parseSkillMd()`'s tolerance since a mis-parsed pipeline EXECUTES) alongside
`{{inputs.x}}`/`{{steps.id.path}}` template resolution
(`server/skills/pipeline-template.js`), built together since author-time forward-
reference validation needs the same reference-parsing the run-time resolver does — 46
pure-logic tests covering every rejected construct, whole-value type preservation on a
template reference, embedded-reference stringification, and the specific no-recursive-
expansion safety property (a tool result containing literal `{{...}}` text must never
be re-interpreted as a new template). Two deliberate deviations flagged to the user at
the time: `validatePipeline()`'s `knownToolNames` is an injected parameter rather than
self-fetched (importing `capabilities.js` from `server/skills/store/` would recreate the
exact deadlock the Phase 1 split exists to prevent, one hop further out), and static
validation also catches an unknown `{{inputs.x}}` reference, not just the step-id case
literally asked for. Phase 4 built `server/skills/pipeline.js`'s `runPipeline()`
(sequential, `continue_on_error`, per-step timeout, a 20-step cap, and a defense-in-
depth re-check of every step's tool target via `tools/index.js` directly — independent
of whether author-time validation ever actually ran), plus
`server/tools/approve_skill_pipeline.js` and `capabilities.js` now injecting `invoke`
itself into every capability's `ctx` (closing the circular-import gap the same way
`ctx.reservedSkillNames` already did). Found and fixed a real gate hole before it
shipped, not after: `Replace` (swapping a Skill's content via a new zip/md) wasn't
resetting `pipelineApproved`, which would have let untrusted uploaded content silently
inherit an existing folder's prior trust — both `replaceSkillContents` and
`replaceSkillMarkdown` now always force it back to `false`, directly verified (not just
reasoned about) when the user later asked for confirmation of this specific claim. Also
caught, by the integration test failing honestly rather than by review: the plan's
"refuse under `autoConfirm`" rule for a confirm-requiring pipeline hadn't actually been
implemented on the first pass, since `capabilities.js`'s existing `autoConfirm` bypass
happens BEFORE a capability's own `run()` is ever called — fixed, re-verified. 20
stub-based unit tests plus 29 real end-to-end tests against `capabilities.js` with 7
actual Skill folders on disk, covering every scenario in the plan's checklist including
the inherited `run_skill_script`/`scriptsApproved` gate through a pipeline step. Phase 5
closed the Phase-4-flagged gap for real rather than leaving it documented:
`readSkillMd()` no longer throws for a Skill with no `SKILL.md` at all (a valid,
pipeline-only shape); `GET /api/skills/:name` now returns full pipeline info with each
step tagged `needsConfirm` via `pipeline.js`'s own `confirmRequiringSteps()`, so the UI
can never disagree with the real run-time gate; `_skill-detail.js` gained a Pipeline
card (status/inputs/steps, or plain-language parse errors) with deliberately no approve
button, matching the existing `scriptsApproved` precedent that real consent belongs in
conversation, not a checkbox; `skills.js`'s list rows gained a `Pipeline` badge.
Verified live across all 4 pipeline states (valid+approved, broken, unapproved,
confirm-requiring) plus a plain `SKILL.md`-only control, screenshots inspected, not just
asserted. Every phase ran on a scratch `JARVIS_DATA_DIR`/unusual port; the user's real
`data/`/`.env`/port 3000 were never touched, and nothing from this session has been
committed or is live yet — the user's server needs both a commit and a restart to pick
any of it up. One thing discovered but not caused by this session, worth flagging:
`server/capabilities.js`/`server/live.js`/`server/models/runner.js` changed on disk
mid-session from what's almost certainly a concurrent second Claude session, adding a
`core`/`unlocked` tool-declaration-slimming layer on top of this session's
`capabilities.js` — built compatibly (`listStepCandidates`, the `ctx.invoke`
self-injection, and the confirm gate are all intact), but folder Skills are never
tagged `core: true`, so a live model may need the new `find_capability` tool to
discover a Pipeline Skill before it can call it by name. Never exercised together; see
Next steps #2.

### 2026-08-19 — Connector credential-prompt regression, "no Client ID/Secret UI anywhere," Gmail via Composio, two risk-classifier bugs
See `handoff-archive.md` § "Connector credential-prompt regression, "no Client ID/Secret UI anywhere," Gmail via Composio, two risk-classifier bugs".

Fixed the reported regression (a "Have a Client ID?" link gated on `!guide` instead of
a real per-connector fact, showing unconditionally on every OAuth connector) plus a
duplicate-URL connector-hijack bug and a silently-dropped-secret bug. Live-verified,
against the user's own real data and each service's own OAuth metadata, that
Google/GitHub genuinely publish no `registration_endpoint` (Notion does) and that even
Composio's one-click experience came from a Client ID entered once, not true DCR. Built
`catalog-credentials.js` (register a Client ID once per catalog entry, every future
connector pre-seeded from birth) — then, on the user's final explicit call, deleted
every Client ID/Secret field, link, and modal from the app outright instead. Connected
the user's real Gmail through their existing Composio connector (Composio's own
pre-registered app, zero Google Cloud setup) — confirmed Active. Found and fixed two
real, previously-undetected bugs while investigating a "confirmation loop" report: a
confirm-token redemption that required byte-identical resent arguments (now runs with
the originally-captured args instead), and a `classifyActionRisk()` bug that lowercased
an identifier before checking its camelCase boundary, always misclassifying a tool like
Composio's own generic dispatcher as risky regardless of what it actually did.

### 2026-08-19 — Model system review, task-aware routing + live health badges, cleanup pass, Add-Model UI + duplicate prevention
See `handoff-archive.md` § "Model system review, task-aware routing + live health badges, cleanup pass, Add-Model UI + duplicate prevention".

Four plan-approved, live-verified passes in one session. (1) Reviewed connections/
models/adapters/routing/failover/cooldowns/refresh; confirmed functional auto-recovery
already worked, but the Models screen's health badge was a stale one-time snapshot —
fixed with a transition-only `model_health` SSE broadcast + debounced
`refreshIfActive()`. (2) Landed approved task-aware routing: new zero-dependency
`server/models/task-types.js`, threaded as an optional `type` on `router.js`/`ai.js`,
kept as a separate axis from `profile` per explicit user decision. (3) Re-scoped 4
deferred cleanup items (3 false alarms, 1 — `AVAILABILITY_STATE_FOR_KIND` — consolidated
into `error-kind.js`) and investigated a reported "blinking"/flaky-availability
behavior — found no bug, just cheap-Test-vs-heavy-real-turn quota mismatch, plus one
bounded cosmetic double-render flagged and left alone. (4) Four Add-Model-screen UI
improvements (live selected count, per-connection count badge, filter dropdown, button
reposition) plus duplicate-model prevention — the button-position request was
**initially misread** (moved the modal's own footer instead of the real page-level "+
Add a model" button), caught by the user via screenshots, reverted, and corrected;
duplicate prevention investigated first (report-only) then fixed on request:
`addModel()`/`discoverModels()` now guard on `(connectionId, model)`, never `model`
alone. All verified via `agent-browser` against isolated scratch servers — real
server/data never touched, no commits made. `CLAUDE.md`'s Model system section still
needs updating with these facts (Next steps #7).

### 2026-08-18 → 2026-08-19 — Chat Persistence + Memory built (SQLite)
See `handoff-archive.md` § "Chat Persistence + Memory built (SQLite)".

Two explicit stages per the user's own spec, review gate between them. Mid-planning
pivot from plain JSON to SQLite (`node:sqlite`, live-confirmed zero-install) after the
user asked directly what was being used. Stage 1: `db.js`/`chat-store.js`,
`conversation.js`'s `bindSession`/`hydrate`, `brain.js`'s `getActiveSessionId()`
replacing the hardcoded `'main'`, a new Chat History screen — one real bug found
(a route's object-literal evaluation order), verified via scratch server + restart
test, confirmed working by the user. Stage 2: `memory-store.js`/`memory-policy.js`/
`memory-review.js`, four fire-and-forget checkpoint kinds, schema-enforced
conversation/memory independence, the `profile.json` migration, five new skills, an
in-chat review card — verified live against a real Gemini key (migration, a full
skill round trip, real extraction + conflict detection, all three conflict-resolution
paths, restart survival, the review card through `agent-browser`); two real bugs found
and fixed (a stray-backtick template-literal corruption in `db.js`; a stale candidate
count in the review card). Followed by a CLAUDE.md documentation pass (new "Chat
Persistence"/"Memory" sections, new `server/memory/CLAUDE.md`, extended meta-skills
list) that also caught and fixed a stale comment in `db.js` left over from the JSON
plan the SQLite pivot superseded. The user's own live server independently restarted
mid-session twice, unrelated to this session's actions — now a permanent Gotcha in
root `CLAUDE.md`.

### 2026-08-18 — CLAUDE.md split into 11 files
_(reconstructed from on-disk evidence; no plan file exists for this session)_

Root `CLAUDE.md` plus one per `public/` and nine `server/` subfolders (mtimes
11:30–12:13). Standing architecture moved out of the root file into the
folder it describes; nearly all session-narrative prose was dropped in the
process — a repo-wide grep for narration phrasing returns exactly one hit
across all 11 files. Two of the new files (`CLAUDE.md`,
`server/connectors/CLAUDE.md`) cite specific sessions in `handoff.md` by name
for incident history — this restructure repoints both citations to
`handoff-archive.md`, since that's where the actual narratives now live.
This session's own audit (this restructure) found 15 claims in the new
nested docs that don't match current code — see Next steps #3.

### 2026-08-18 — MCP connector registration overhaul (CIMD, DCR fallback, Gmail zero-tools bug) + a UI correction
_(reconstructed from plan file `plans/the-current-mcp-connector-recursive-metcalfe.md`; not written during the session)_

Parts 1–4, live-verified per the plan's own status block against real
`data/connectors.json`: a DCR refusal (Lovable rejects non-partners; Slack
has no registration endpoint) no longer throws — it surfaces a manual-
Client-ID fallback instead of dead-ending the connector; new
`server/connectors/client-identity.js` adds Client ID Metadata Document
(CIMD) support, the current MCP spec's *primary* registration mechanism
(DCR is spec-deprecated), tried first when a public HTTPS address is
configured; Gmail/Drive's false "no auth needed" conclusion (their servers
answer unauthenticated but gate real tool calls) is fixed and repaired for
records already saved wrong. Part 5, a UI correction after comparing the
shipped page to real Claude screenshots — the divergence between this part's
written design (inline fields once justified) and what actually shipped (a
separate link+modal) was never fully reconciled and kept resurfacing; **finally
resolved 2026-08-19** (see that entry above) by removing Client ID/Secret UI
entirely, superseding both versions.

### 2026-08-18 — Guided-connector credentials fix (existing Slack record)
_(reconstructed from plan file `plans/reconnection-glitch-when-i-velvet-sloth.md`; not written during the session)_

Narrow, data-driven bug: the "match Claude's architecture" round removed
*all* guide-reading from the connector detail page, including for a
connector whose record already has `connectFlow.guide` stored (created via
Browse Connectors, e.g. the user's real Slack connector) — leaving only a
generic "remove and re-add" message with no way to enter credentials, even
though every needed detail was already on the record. Fixed by giving
`buildMcpConnectSection` (`public/screens/_connector-detail.js`) back a
narrow `connectFlow.guide`-present branch. Distinct from the earlier,
correctly-fixed mistake of showing credential fields speculatively on every
connector — this only renders when the record's own data already says so.

### 2026-08-18 — composer/conversation-panel layout fixes + rail-width narrowing
See `handoff-archive.md` § "composer/conversation-panel layout fixes + rail-width narrowing".

Two real, distinct bugs in the same area, found via live measurement against
the real running server rather than reading CSS. Bug 1: the composer's own
growth pushed its Send button below the visible panel — fixed by moving the
height cap off the wrapper and onto the two regions that actually grow
(attachment grid, text box) instead of one shared scroll container. Bug 2:
the control row visually overlapped multi-line typed text — fixed with
`flex-wrap` forcing the textarea onto its own line, no DOM changes. Rail
width narrowed 640px → 540px → 500px on direct request, each confirmed live.

### 2026-08-18 — App Control connector UI rebuilt to match Claude, then a general auto-updating icon system
See `handoff-archive.md` § "App Control connector UI rebuilt to match Claude, then a general auto-updating icon system".

Round 1: bug fixes (header CSS eating clicks, raw MCP descriptions leaking
into the UI) then a rebuild against real Claude Desktop screenshots — kept
Jarvis's MCP/API/CLI tabs, kept Disconnect red, rebuilt the permission
control into a real 4-state dropdown. Round 2: built
`server/connectors/icon-resolver.js` from scratch after the user rejected
hardcoding one more per-service SVG — MCP server's own declared icon, then a
favicon fetched server-side and cached as `data:`, then a generic glyph. Two
"gated on the wrong status" bugs found via the user's real data (Lovable's
icon never resolved because it was gated on `status:'working'`).

### 2026-08-16 — Skills system rebuilt from scratch, then three real follow-up bugs
See `handoff-archive.md` § "Skills system rebuilt from scratch, then three real follow-up bugs".

Full rebuild against Claude's real `SKILL.md` spec after the Skills screen's
"Browse skills" catalog was found offering Jarvis's own native abilities
(e.g. `webapp-testing`) as installable third-party skills — the third time
this bug class had appeared. `server/skills-fs.js` and the bundled catalog/
link-install mechanism deleted outright; every skill/tool entry now carries
`kind: 'builtin'|'skill'|'connector'`, and `listUserSkills()` is the only
function any Skills UI may call — structurally impossible for a native
ability to leak in again. Three follow-up bugs, each caught by testing the
real browser flow: upload only accepted `.zip`; drag-and-drop never
populated the file input; `h5`/`h6` were muted gray, breaking a common
"numbered sub-step" heading pattern.

### 2026-08-15 — attachments: composer redesign + real file support
See `handoff-archive.md` § "attachments: composer redesign + real file support".

Composer chips replaced with real thumbnail/type-badge tiles. New
`server/documents/` reads `.docx`/`.xlsx`/`.pptx` into Markdown with no new
dependency — every model can now read an Office document, not just Gemini.
`analyze_spreadsheet.js` runs a model-written script in the sandbox against
a full oversized workbook rather than truncating it. Two real bugs found
only against downloaded fixture files: an embedded-image filter used file
size as a furniture-vs-photo proxy and dropped real photos; chart-data
extraction read the XML tree one level too shallow and silently produced
empty tables every time.

### 2026-08-14 — floating conversation rail + orb-stability fix
See `handoff-archive.md` § "floating conversation rail + orb-stability fix".

The conversation panel un-boxed from a flex-column sibling (which was
pushing the orb/composer off-centre) into an absolutely-positioned floating
rail with symmetric stage padding so it can never overlap the centred
content. `#orb-stage` pulled fully out of flow so a growing composer can no
longer shrink or shift the orb. A live-measurement-only bug: `#mic-hint`'s
default paragraph margin was stacking on top of the flex `gap`, silently
costing ~28px the reserve hadn't budgeted for.

### 2026-08-13 — voice/conversation UI overhaul (orb, composer, dictation) + three rounds of voice-pipeline fixes
See `handoff-archive.md` § "voice/conversation UI overhaul (orb, composer, dictation) + three rounds of voice-pipeline fixes".

Part 1: side conversation panel, auto-growing composer, dictation mic, a
real 3D orb (vendored three.js), a self-listening fix. Part 2: the user
reported the voice experience had regressed — four regressions traced to
exact lines and fixed (recognition suspended too early, a barge-in warm-up
that slowed interruption, `LiveEngine`'s native barge-in accidentally
disabled, an unneeded `AnalyserNode` tap on TTS playback), plus a stale-
timestamp defect that could make the barge-in gate fire almost instantly on
the *next* reply. Part 3: two mute-button bugs. Part 4: a CSS-only
horizontal-scrollbar fix.

### 2026-08-05 → 2026-08-13 — connector/permissions full rebuild, MCP full rebuild, Stage 3, Stage 4
See `handoff-archive.md` § "connector/permissions full rebuild, MCP full rebuild, Stage 3, Stage 4".

The largest single track. Started from the user asking whether "standing
permission" and "runtime confirmation" — two paragraphs from their own spec
— had really been kept separate; the answer was no, leading to "rebuild
everything… i don't want to do this again." Two rebuild passes (the first
left MCP's own transport code as legacy under cosmetic tweaks; the user
caught it and a second pass rewrote that too) produced the current
three-peer-mechanism (MCP/API/CLI) connector system. Then Stage 3 (desktop
control gains close/restore/arrange window + scratch-window auto-cleanup +
a Start-Menu launch fallback + connector tools mid-session, plus the whole
monitoring system) and Stage 4 (an isolated sandbox — WSL/restricted
backends — plus a Skills marketplace v1 fully replaced by the 08-16 rebuild
above).

### 2026-08-05 — Connector detail pages, per-tool permissions, guided setup
See `handoff-archive.md` § "Connector detail pages, per-tool permissions, guided setup".

Richer connector list + a real per-connector detail page + a bigger catalog
(Gmail/Notion/Slack/Google Drive/GitHub), each verified live against real
OAuth discovery documents first. A real RFC 9728/8414 compliance bug found
this way: GitHub's protected-resource metadata lives under a path-aware
well-known URL, not the origin root Notion/Slack use — found by reading a
real 401's `WWW-Authenticate` header. A tool-grouping bug found by
connecting a real service: it only matched a verb anchored to the start of
a tool name, putting Notion's service-name-first tools all into "Other".

### 2026-08-04 — Desktop control / Browser / Files settings removed from the interface entirely
See `handoff-archive.md` § "Desktop control / Browser / Files settings removed from the interface entirely".

The user asked directly whether Desktop control/Browser/Files should ever
have had a settings UI at all — interviewing surfaced that a rule already
written into CLAUDE.md for the Skills screen had simply never been applied
here. All three cards deleted; the safety blocklist became fixed non-editable
defaults; the Files folder allowlist now grows only through conversation via
new `allow_folder.js`. A real bug this surfaced: the Files/Browser singleton
connector records used to only ever get created as a side effect of the
now-deleted cards' page-load fetch — fixed by seeding both unconditionally at
server startup.

### 2026-08-04 — App Control's Connectors rebuilt to match Claude's
See `handoff-archive.md` § "App Control's Connectors rebuilt to match Claude's".

The user compared the previous "Connected services" UI against real Claude
Desktop screenshots and called exposing CLI/HTTP as separate user-facing
types a genuine design mistake. Rebuilt to exactly two ways to add an
integration (Official Connectors directory, Custom Connector by MCP URL),
implementation details hidden from the user entirely. New `oauth.js`
(OAuth 2.1 + PKCE, RFC 7591 DCR) and `mcp-remote-client.js` (Streamable
HTTP). Catalog trimmed to Notion only, on purpose — "catalog honesty":
an entry ships only once its connect flow is verified end to end.

### 2026-08-04 — Content Analysis + Planning Partner rebuilt from a refined spec
See `handoff-archive.md` § "Content Analysis + Planning Partner rebuilt from a refined spec".

Both engines deleted outright and rebuilt against a user-supplied spec after
an audit found the *shape* of both was the problem, not a fixable bug on
good foundations: Content Analysis ran a fixed fact-checking template on
every piece of shared content before ever asking what was wanted; Planning
auto-advanced through a fixed question queue that misfiled a challenge to
the idea as "the answer to question N." The `questions[]`/`answers{}` queue
is gone entirely, replaced by `decisions[]` — one entry per thing actually
settled. At the time this was written, everything was verified against a
scripted stub model only, not a real one — worth confirming that's since
been exercised live if this area comes up again.

### 2026-08-04 — Planning + Content Analysis folded into the conversation
See `handoff-archive.md` § "Planning + Content Analysis folded into the conversation". Superseded by the session above — kept for the historical design reasoning (one shared transcript, no separate pages) which is still current.

### 2026-08-03 — Planning + Content Analysis, built as pages
See `handoff-archive.md` § "Planning + Content Analysis, built as pages". Superseded twice over — kept for the foundational pieces (`ai.js`, `research.js`, `media.js`, the capability-gating design) which are all still current.

### 2026-08-02 → 2026-08-03 — App Control / Skills / Computer Control / App Control connectors (Stages 1–3 of 4)
See `handoff-archive.md` § "App Control / Skills / Computer Control / App Control connectors (Stages 1–3 of 4)".

Foundational track: playbook Skills + the safety spine (Stage 1), the
look/act/verify computer-control loop (Stage 2 — infrastructure verified,
one unbroken happy-path run still not watched to completion at the time), and
the first version of App Control connectors, all five original types
live-tested (Stage 3). Contains the Notepad shared-process warning
(`Stop-Process` on a disposable test window closed the user's real, unrelated
Notepad window — Windows 11 Notepad can share one process across windows).
`git init` had happened by the end of this session but there were still zero
commits — **still true today**, verified this session (`git rev-list --count
HEAD` errors).

### Earlier — model discovery, availability tracking, task popup
See `handoff-archive.md` § "model discovery, availability tracking, task popup" (titled "The session before that" there).

Round 1: every provider adapter gained real `listModels()`, tiered
per-failure-kind health cooldowns replaced one flat 5-minute cooldown, the
task-creation popup was rebuilt wider against a Grok/Claude reference blend.
Round 2, after the user tried it live: a Free/Paid filter, Connectors
downgraded to an inert "coming later" badge, Skills removed from the popup
entirely (a different, later feature was planned for that), the per-
connector permission notice collapsed to one fixed blanket message. A real
flexbox bug found only by live measurement: an `overflow:hidden` element
inside a flex column was the only sibling allowed to shrink under pressure,
crushing the instructions box to ~2px.

### Earliest recorded — connections, task/briefing popups, per-task model pinning
See `handoff-archive.md` § "connections, task/briefing popups, per-task model pinning".

The foundation everything above was built on: multi-model support across any
provider, task scheduling, a morning briefing, voice-mishearing confirmation,
drawer navigation, grouped model "connections," popup-based creation, and
per-task model pinning with the same fallback chain as the rest of the app.

## Where else to find things

- **Root `CLAUDE.md`** — the hub. Architecture overview, run/test
  instructions, and pointers into the 10 per-folder `CLAUDE.md` files
  (`public/`, `server/connectors/`, `server/content/`, `server/control/`,
  `server/documents/`, `server/monitor/`, `server/projects/`,
  `server/sandbox/`, `server/scheduler/`, `server/skills/`) — each holds the
  standing architecture/gotchas for that folder. Read the relevant nested
  file, not just the root, before changing code in that area.
- **`handoff-archive.md`** — every session's full narrative, verbatim.
- **Persistent memory** (`C:\Users\HP\.claude\projects\...\memory\`,
  indexed by `MEMORY.md`) — cross-session facts about the user and project
  that aren't code-derivable.
- **`C:\Users\HP\.claude\plans\`** — every plan file this project has ever
  produced. Several recent ones (the two reconstructed sessions above, plus
  a handful of older ones) were never written into either handoff file at
  all — if a plan file's topic isn't in the session log above, check the
  plans folder directly before assuming nothing happened.
- **`How to Use Jarvis.md`** — user-facing instructions; may drift behind UI
  changes (e.g. Skills/Connectors wording) faster than this file does.

## The pruning rule

When a session ends: its durable lessons (things that will matter to future
unrelated work — a gotcha, an architecture fact, a permanent design rule) go
into the relevant `CLAUDE.md`, not here. Its handoff entry gets the 3–6 line
log form above, linking to `handoff-archive.md` for anyone who wants the full
story. **This file should never grow past a length a cold session will
actually read in full** — that's what happened last time (2158 lines) and is
why it stopped being updated at all. If a change is big enough to want a full
narrative, write it in `handoff-archive.md` directly and link to it from here
— don't let the narrative accumulate in this file.
