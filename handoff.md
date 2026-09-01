# Handoff — read this first when picking this project back up

For full session narratives, see **`handoff-archive.md`**. This file is
current-state only — kept short on purpose so a cold session actually reads
it end to end. See "The pruning rule" at the bottom before adding to it.

## Right now

**This session (2026-08-31 → 2026-09-01) ran a full audit + remediation, then fixed
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

## Waiting on the user

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
