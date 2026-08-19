# Handoff — read this first when picking this project back up

For full session narratives, see **`handoff-archive.md`**. This file is
current-state only — kept short on purpose so a cold session actually reads
it end to end. See "The pruning rule" at the bottom before adding to it.

## Right now

The most recent work is a **connector credential-prompt regression fix, ending in a
final "no Client ID/Secret UI anywhere" decision, plus a live Gmail connection through
Composio and two risk-classifier/confirm-loop bugs**. Root cause of the reported
regression: the "Have a Client ID?" link was gated on `!guide` (true for every
connector before it had even attempted a connect), not on any real per-connector fact —
fixed to gate on a persisted `connectFlow.manualClient`, live-verified. The user then
pushed back on "Gmail needs a Client ID" itself; verified live against each service's
own OAuth metadata (Google/GitHub publish no `registration_endpoint`, Notion does) and
against the user's own real Composio connector record (`credentialSource:
"preregistered"`, not `"dcr"` — even Composio's one-click experience came from a
Client ID entered once, not true auto-registration). Built `catalog-credentials.js` —
register a Client ID once per catalog entry, every future connector from it is
pre-seeded from birth — then, after the user compared the result to Claude's real UI
one more time, made a final call: **removed every Client ID/Secret field, link, and
modal from the app entirely**, for every connector, deleting the now-unused modals
outright rather than just unlinking them. `catalog-credentials.js`'s register-client
route stays reachable server-side only, meant to be called directly (by Claude) rather
than through any screen. Separately, connected the user's real Gmail through their
existing Composio connector (`COMPOSIO_MANAGE_CONNECTIONS` — Composio's own
pre-registered app, zero Google Cloud setup) — real account, real sign-in, confirmed
`ACTIVE`. A follow-up "stuck in an infinite confirmation loop" report surfaced two real,
previously-undetected bugs, neither introduced this session: `skills/index.js`'s
confirm-token redemption required byte-identical resent arguments (broke for any
complex/nested skill args — now runs with the original args, token alone is the proof
of consent); `guard.js`'s `classifyActionRisk()` lowercased an identifier before
checking its camelCase boundary, so a name like `COMPOSIO_MULTI_EXECUTE_TOOL` (the
platform's own generic action dispatcher) was always 'risky' regardless of what it was
actually asked to do. Both fixed and verified with a full regression battery against
the original SharePoint/updatePet cases this project's history already documents. Live
server not yet restarted to pick any of this up. Full narrative below.

Before that, the most recent work was a **model/provider system pass** — four linked
passes (a full system review + a live-health-badge fix, task-aware routing via new
`server/models/task-types.js`, a cleanup pass, Add-Model UI improvements + duplicate
prevention), all plan-approved and live-verified via `agent-browser` against scratch
servers. `CLAUDE.md`'s Model system section still needs updating with these facts
(Next steps #5).

## Next steps

1. **Action the remaining CLAUDE.md staleness findings** — full list in
   `plans/audit-handoff-md-in-this-graceful-thompson.md`. Two of the highest-value
   items (the connector Client ID/Secret UI design, and `client-identity.js`/CIMD) are
   now resolved as part of this session's docs pass — `server/connectors/CLAUDE.md`
   documents both the final "no UI anywhere" state and CIMD/`catalog-credentials.js` in
   full. Still open: `friendly-message.js` and the icon system are undocumented; the
   connector permission model is documented as 2-state but is 4; `CONTROL_TOOLS` is
   documented as flat tools but is 3 tools plus a batched `actions[]` array.
2. **The WSL sandbox backend has still never run against a real WSL distro**
   — none is installed on this dev machine. `server/sandbox/wsl-backend.js`
   is reasoned through carefully but unverified; test for real the first time
   WSL exists here or on another dev machine.
3. **Task A is still not started**: a dedicated verification subagent proving
   Jarvis actually *invokes* a skill in real conversation (not just that
   upload succeeds) — single-skill trigger, correct pick among several, an
   ambiguous case, a no-match negative case, each backed by logs/traces, not
   Jarvis's own claim. User confirmed real API quota use is fine for this.
4. **No AI-driven control session has ever reached `report_done` unbroken**
   end to end (open → act → save → verify → done) — every primitive is
   verified individually; the full happy path with the newer close/restore/
   arrange actions and scratch-window auto-cleanup has not been watched to
   completion.
5. **Root `CLAUDE.md`'s Model system section is now out of date** — it
   doesn't yet mention `server/models/task-types.js` (task-aware routing),
   the `model_health` SSE event, or the `addModel()`/`discoverModels()`
   `(connectionId, model)` duplicate-prevention rule, all landed this
   session. A small, bounded double-render around "Check all models"
   (SSE-triggered re-render racing the button's own) was also flagged as a
   known, low-priority cosmetic item, deliberately left unfixed.

## Waiting on the user

- **An ordinary restart of the user's own live Jarvis** — needed to pick up BOTH Chat
  Persistence + Memory (confirmed read-only that the currently-running instance
  predates Stage 2) AND this session's connector/risk-classifier/confirm-loop fixes
  (none of which are live yet either). Not restarted automatically either time since
  the user was actively using the instance. Once restarted, the specific test steps
  already given in-chat for Chat Persistence/Memory are what to run through for that
  part.
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

## Not yet written up

*(empty — the three sessions that had accumulated here as of this restructure
are now in the session log below, marked as reconstructed.)*

## Session log (newest first)

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
needs updating with these facts (Next steps #5).

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
nested docs that don't match current code — see Next steps #1.

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
