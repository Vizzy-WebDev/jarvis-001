# Handoff archive — full session history, verbatim

**This is the historical record, not the current-state doc.** For "what's in
flight right now / what to do next / what's blocked on the user," read
`handoff.md` instead — it's short and kept current. This file holds the full
narrative of every session that touched this project: what was asked, what
was found, what was built, how it was verified, real bugs hit along the way.
Nothing here is re-verified as it ages — an entry is accurate as of the
session that wrote it, not necessarily as of today. `handoff.md`'s session
log links into specific sections here by heading.

Moved into this file verbatim on 2026-08-18 when `handoff.md` was split in
two (it had grown to 2158 lines / 130KB and stopped being something a cold
session would read end to end). Two CLAUDE.md files cite specific sessions
in this file by name for incident history: root `CLAUDE.md` and
`server/connectors/CLAUDE.md` — both now point here rather than at
`handoff.md`.

---

### 2026-08-19 — Connector credential-prompt regression, "no Client ID/Secret UI anywhere," Gmail via Composio, two risk-classifier bugs

The user reported that a Client ID/Secret prompt was now appearing on every OAuth
connector — Notion, Composio, every custom connector — not just the four that
genuinely need one, and asked directly what the prior "Advanced settings" work had
broken. Root cause, found by reading the actual code rather than re-asserting: the
"Have a Client ID for this service?" link in `_connector-detail.js` was gated on
`!guide`, which is true for every connector *before it has ever tried to connect* —
Notion, Composio, a fresh custom connector — not on any real, per-connector fact. The
Aug 18 plan had specified the correct gate (`guide || manualHint`) but only half of it
shipped; `manualHint` was never wired up. Fixed: the link now gates on
`connector.config.connectFlow.manualClient` (a real, persisted failure reason from an
actual attempt), and a `guide` catalog entry's Connect button tries the automatic chain
first, only falling back to the guided modal once that attempt genuinely returns
`needsManualClient`. Two smaller real bugs fixed alongside it: a duplicate-URL "reuse
the existing connector" shortcut in `POST /api/connectors/custom` wasn't scoped to
`source.type === 'user'`, so a custom connector pointed at a catalog entry's URL could
silently hijack the catalog record and force-rewrite a verified `kind:'none'` connector
into `oauth_guided`, breaking it; and a Client Secret typed without a Client ID was
silently discarded with no error in two places. All verified on scratch servers with
`agent-browser` screenshots before/after.

The user pushed back hard on the underlying "Gmail needs a Client ID" explanation,
citing their own screenshot of Claude's real "Add custom connector" dialog and asking
why Composio/Notion don't need this. Verified live, repeatedly, against each service's
own OAuth server metadata (not assumed): Google's and GitHub's authorization servers
publish no `registration_endpoint` at all (18 and 8 keys respectively, neither list
includes it); Notion's does (`https://mcp.notion.com/register`). The user's own live
Composio connector record was the clinching piece of evidence — `credentialSource:
"preregistered"`, not `"dcr"`, meaning even Composio's one-click experience came from a
Client ID typed in once, not true automatic registration; the difference is which
service already went through that step, not a rule in Jarvis's code. Confirmed
separately that `oauth.js`'s `startConnect()` never reads `connectFlow.kind` at all —
every connector, guided-labeled or not, already runs the identical CIMD→DCR discovery
chain.

Built the actual fix for "make it permanent like Composio": `server/connectors/
catalog-credentials.js` (new) — one Client ID/Secret per catalog entry, registered once
via `POST /api/connectors/catalog/:catalogId/register-client`, then automatically
seeded onto every future connector `server.js`'s `/ensure` route creates from that
entry (`connectFlow.clientId` plus a fresh `connclient_<id>` secret, both written before
the connector record is even returned) — so a second Gmail/Drive connector, even after
Remove-and-re-add, is pre-registered from birth and never shows a prompt. Client ID
lives in `data/catalog-credentials.json` (git-ignored); the secret goes through the
existing `saveSecret()`/`.env` mechanism, never the JSON file. Verified end to end on a
scratch server: register once, `/ensure` a second connector, confirm both the client ID
and — checking the actual `.env` file directly, since `hasSecret` in the public API
reflects the OAuth token ref, not the client secret ref — the secret were both already
present with no user input.

Shown this working correctly on their real Google Drive connector (a real Google
rejection message, no field to act on it), the user made a final, explicit call:
**remove every Client ID/Secret field, link, and modal from the app entirely, for every
connector.** Not the earlier "only show it when earned" version — none of it, anywhere,
ever. Implemented: the Add-custom-connector "▸ Advanced settings" toggle deleted from
`app-control.js`; `buildGuidedSetupModal()`/`buildManualClientModal()` (and their
now-unused helpers `fetchRedirectUri()`/`buildRedirectAddressBox()`) deleted outright
from `_connector-detail.js`, not just unlinked; Browse Connectors' guide-entry
special-case removed so every catalog row goes straight to the same plain detail page.
`oauth.js`'s `manualClientHint()` messages rewritten — they used to say "enter it
below," which no longer means anything with no field to enter it into. Server-side
`register-client` route deliberately kept, meant to be called directly (by Claude, given
real credentials outside the app's own UI) rather than through any screen — the one
remaining path to ever connect Gmail/GitHub/Slack/Drive. Verified live: a fresh
connector now shows a bare Connect button with nothing else, and a connector with a real
recorded failure shows the honest reason as plain text, no link. See
`server/connectors/CLAUDE.md`'s "No Client ID/Secret UI anywhere" for the full four-
round account of this regressing and finally landing here — flagged as a hard rule,
never re-add any form of it without being asked again.

With Composio's real, working connector already in hand, tested whether the
"Composio/Smithery/Zapier are hosted gateways with their own pre-registered apps" idea
the user raised was actually usable today. It was: `COMPOSIO_MANAGE_CONNECTIONS` (one
of Composio's own 7 tools, already live) accepts `{toolkits: [{name: "gmail", action:
"add"}]}` and returns a real Composio-branded sign-in link — no code change needed,
just calling an already-exposed tool directly via `mcp-remote-client.js` (bypassing
Jarvis's own chat loop, so as not to inject a scripted message into the user's real
conversation). The user clicked the real link and signed in with their real Google
account; `COMPOSIO_WAIT_FOR_CONNECTIONS` confirmed `status: "ACTIVE"` against the user's
real Gmail address. Gmail is now genuinely usable through Jarvis, today, with zero
Google Cloud setup — through Composio's own pre-registered app, not Jarvis's direct
Gmail catalog entry.

Investigating a follow-up report ("stuck in an infinite confirmation loop... only
dangerous commands should need confirmation, and I should be able to confirm by
talking") surfaced two real, previously-undetected bugs, neither introduced today:

1. **`skills/index.js`'s confirm-token redemption required the model's "yes, do it"
   call to resend byte-identical arguments** to the original ask (a sorted-JSON
   equality check). Fine for a skill with one or two simple strings; broken for
   anything with a complex/nested shape, since a model regenerating "the same" call has
   no guarantee of reproducing an identical object — the equality check silently
   failed, minted a fresh token, and asked again, which is indistinguishable from an
   infinite loop no matter how many times the user said yes. Fixed by trusting the
   token alone as proof of consent and running with the ORIGINAL args captured when the
   token was issued, never whatever gets resent — removing the fragile check entirely
   rather than loosening it. Verified with a real skill (`remember_about_me`): asked,
   "confirmed" with deliberately different args, ran using the original text; reusing a
   spent token correctly asked fresh rather than running twice.
2. **`classifyActionRisk()` (`guard.js`) lowercased an identifier before ever checking
   its camelCase boundary** — undoing, a second time, the exact fix this file's own
   history already made once. `const kind = ...toLowerCase()` ran before `kind` reached
   `words()`, so `words()`'s own correctly-written boundary-split regex had no
   uppercase letters left to find one at — `COMPOSIO_MULTI_EXECUTE_TOOL` (the
   platform's own generic dispatcher tool, the one real tool that runs virtually
   everything asked of it) was always classified 'risky', not for anything it was
   doing, but because "execute" is a structural word-part of its own name. Every action
   through Composio needed confirmation regardless of what it actually was — the
   reported symptom. Fixed by keeping the real casing all the way into `words()` for
   the identifier (`kind`) specifically, while deliberately lowercasing prose
   (`label`/`description`) *before* tokenizing instead — running the same camelCase
   split over natural language wrongly breaks proper nouns like "SharePoint" into
   "share"+"point", the exact false-positive class this file was already rewritten once
   to avoid. Separately, `classifyToolRisk()` (`connectors/index.js`) now trusts a
   tool's real description over its bare name once one exists, so a generic dispatcher
   whose *name* happens to contain a strong verb isn't judged by that alone. Verified
   with a full regression battery (real Composio tool descriptions, real Notion
   tool descriptions, the original updatePet/SharePoint/sidebar-order cases this
   file's history already documents) — nothing that was risky before is risky now,
   and the false positives are gone. Noted as an honest, un-closed gap: a static,
   per-declaration classifier still can't see a dispatcher's actual arguments, so a
   genuinely dangerous action performed *through* Composio (not by calling it directly)
   won't trigger confirmation either — real per-call risk classification would be the
   fix if that's ever needed.

Both classifier fixes and the confirm-loop fix are in code only as of this session's
end — the user's live server needs a restart to pick them up; not restarted
automatically since the user was actively using it.

### 2026-08-19 — Model system review, task-aware routing + live health badges, cleanup pass, Add-Model UI + duplicate prevention

Four linked passes over the model/provider system, each explicitly plan-approved before
implementation, each verified live rather than by reading code.

**Pass 1 — review, no changes.** The user asked for a full review of connections/models/
adapters/routing/failover/cooldowns/refresh before touching anything, plus two specific
questions verified rather than assumed: does Jarvis auto-recheck a failed model, and is
routing actually task-aware. Findings: the three-layer architecture (adapters /
connections+models / routing+execution) was already clean and worth keeping as-is;
**functional** health recovery already worked automatically (`health.js`'s `isHealthy()`
lazily expires a cooldown on the next call, no timer needed) — the real gap was that the
Models screen's badge was a one-time fetch snapshot that never reflected a later
recovery without a manual reload/Test/Check-all click; and task-awareness was thin (only
a `chat`/`control` `profile` plus a regex-guessed `complexity`, no real per-domain task
type). Three Explore agents traced this in parallel, spot-checked directly against
source afterward.

**Pass 2 — approved design, implemented.** Smallest-viable fix for both gaps: (1)
`health.js`'s `markUnhealthy`/`markHealthy` now `broadcast()` a `{type:'model_health'}`
event over the existing `/api/events` SSE bus, but only on a genuine transition (a
`wasHealthy`/`wasUnhealthy` snapshot taken before the mutation) — `public/router.js`
gained `refreshIfActive(sectionId)` (debounced ~400ms), wired from `app.js`'s SSE
dispatcher, so the badge now updates live with zero user action. (2) New
`server/models/task-types.js` — zero imports, so it's automatically skills-safe — defines
`coding | research | vision | simple-question | general`, each with a default capability
`need` and a scoring lean expressed in the same `'reasoning'|'quick'` vocabulary
`router.js`'s `complexity` already used, so no parallel classifier was needed. Threaded
as an optional `type` on `router.js`'s `profileTask()` and `ai.js`'s `candidatesFor()`/
`askModel()`/`pickModels()` — declared wins, else inferred from text, exactly mirroring
existing behavior for callers that don't pass it. Three explicit user decisions locked
the design: `profile` (chat vs. control — orchestration context) and `type` (task
domain) stay as two independent axes rather than folding one into the other;
`computer-control` stays out of the `type` enum; the 4 lower-priority items below were
left for a separate pass rather than bundled in. Live-verified in a follow-up turn via
`agent-browser` against a scratch stub server + scratch Jarvis instance: toggling a
model's health from outside the open browser tab (so only the SSE path, not a button's
own re-render, could be responsible) flipped the badge live in both directions, zero
console errors.

**Pass 3 — cleanup + behavior investigation.** Re-scoped the 4 previously-deferred
inconsistencies: tier/capability read-time asymmetry and the openai-compatible billing
quirk were both confirmed deliberate/harmless (re-deriving tier live would silently
clobber a user's manual edit; the billing "quirk" is one adapter using better real data
where available, uniformly falling back otherwise) — left alone. `friendlyError()`'s
differing per-adapter unwrap depths matched real, different SDK error shapes and never
reach the user anyway (logged only) — left alone. The triplicated
`AVAILABILITY_STATE_FOR_KIND` literal (`runner.js`/`ai.js`/`server.js`, byte-identical
today but with no structural guarantee of staying that way — one copy's own comment
justifying the duplication was already stale) was consolidated into `error-kind.js` (a
zero-import leaf module all three already depended on for `classifyError`), all three
now import instead of redeclaring. Separately investigated — per explicit instruction,
report-only, no fix — a reported "model shows Ready right after a check, Unavailable
again minutes later" and "the screen seems to blink." Traced and explained: Test and
"Check all models" call the identical `testAndRecord()` function per model, so their
agreement is by design; a manual Test is a tiny, cheap, non-streaming probe (no system
prompt, no tool declarations, no history) while a real turn is a much larger payload —
free-tier limits are usually token-based, so the cheap test can pass comfortably while a
real turn blows the same quota minutes later, a genuine effect of the CLAUDE.md-documented
"rate-limiting is the normal state" reality, not a bug. No continuous re-render mechanism
was found (exactly one `EventSource` per tab, no background timer calls a model
frequently, the new broadcast/debounce logic checked correct) — the one real, bounded
artifact found was a possible double-render right around a "Check all models" click
(the SSE-triggered debounced render racing the button's own post-completion render),
flagged as a known minor cosmetic item and explicitly left unfixed per the user's scope.

**Pass 4 — Add-Model UI improvements + duplicate prevention, with a real misread
caught and corrected.** Four UI requests, disambiguated via inspection into two
different surfaces the literal wording actually pointed at (confirmed by exact label
matching, not guessed): a live "N selected" count and moving an "Add" button both could
only mean the Add-Model checklist modal (`buildModelPicker`); "All/Free/Paid/Working
now/Not working" as one dropdown matched, by exact label text, the *main* screen's own
filter bar, not the modal's different (Free/Paid-only) filter. Implemented: the live
count (a delegated `change` listener plus explicit calls from Select-all/Select-none,
since setting `.checked` in JS doesn't fire a native event); a persistent per-connection
`connection.modelCount` badge (previously only ever surfaced transiently inside the
"Remove connection?" confirm text); `_ui.js`'s existing `dropdownControl()` swapped in
for the main screen's `segmented()` filter bar. **The 4th item — "move the Add button to
the top" — was initially misread** as the modal's own Cancel/Add footer (added a
`footerPosition` option to `_modal.js`, moved it there, verified working) instead of what
the user actually meant, shown via two screenshots of their real, long model list: the
large standalone page-level "+ Add a model" button, stranded at the very bottom after
potentially dozens of models. Caught, `_modal.js` reverted byte-for-byte, and the real
button moved to sit right above the search/filter row instead — the general lesson: a
vague spatial instruction ("move X to the top") given without screenshots is worth a
concrete confirming question or a very literal re-read once corrected, since two
plausible targets existed in the code and the wrong one was picked first. **Duplicate
prevention** was investigated first (report-only, per instruction) — confirmed a true
`(connectionId, model)` duplicate could be created via both add flows, since the only
uniqueness ever enforced was the internal `id` string, not any semantic field — then,
once the user said "fix part 1," built: `addModel()` in `registry.js` now throws on an
existing `(connectionId, model)` pair (self-protecting within a batch too, since
`addModels()`'s loop re-`load()`s after each save); `discoverModels()` now filters
already-added models out of its own results when re-discovering for an existing
connection, scoped strictly to that `connectionId` so the same model name under a
*different* connection (the user's explicit two-API-keys requirement) stays fully legal;
a defensive error surfaced in the modal for the one path discovery-filtering can't cover
— manually typing an already-added model's name. Verified with direct pure-logic calls
(duplicate correctly thrown, legitimate cross-connection case correctly succeeded) and a
full `agent-browser` pass (discovery list correctly excludes the already-added model,
typed-duplicate shows a clear error and keeps the modal open, a genuinely new model still
adds successfully end-to-end, connection badge count updates correctly).

All four passes verified via isolated scratch Jarvis instances (separate port,
`JARVIS_DATA_DIR`, `JARVIS_ENV_PATH`) plus small `node:http` stub servers where a real
provider call wasn't needed — the user's real server/data was never touched. No commits
were made; all changes are on-disk only. `CLAUDE.md`'s Model system section has not yet
been updated with the new `task-types.js`/`model_health` event/duplicate-guard facts —
worth doing before this area is touched again.

### 2026-08-18 → 2026-08-19 — Chat Persistence + Memory built (SQLite)

The user's own mega-prompt spec'd two stages, explicitly in order with a review gate
between them: **Chat Persistence** (conversations survive a restart, a Chat History
screen) then, only after that was confirmed working, **Memory** (a curated,
approval-first fact store). The prompt itself asked for interviewing first — the real
goal restated back, the proposed structure evaluated rather than built as-is, before
any code — which surfaced the actual design pressure underneath both stages: Memory's
write path needs to be a single funnel with one policy decision point, since a future
self-improvement system (the user's own stated reason for building this at all) will
eventually plug into that same seam.

**The storage decision pivoted mid-planning.** The initial plan used plain JSON, matching
every other `data/*.json` store in the project. The user asked directly whether SQLite
was being used. Rather than answer from assumption, `node:sqlite` (built into this
project's Node 24, confirmed via `node --version`) was tested live in the scratch
directory — FTS5 full-text search and WAL mode both worked, zero install — before
settling on SQLite for both Chat History and Memory, a deliberate one-store exception to
the JSON-everywhere convention. See root `CLAUDE.md`'s "Chat Persistence" and "Memory"
sections for the resulting architecture; `server/memory/CLAUDE.md` for Memory's
module-by-module breakdown.

**Stage 1** — `server/db.js` (the one SQLite connection, migrations keyed on
`PRAGMA user_version`), `server/chat-store.js` (conversation/message CRUD + FTS5
search), `conversation.js`'s new `bindSession`/`hydrate` (full transcript on disk, the
existing 60-message window still all a model ever sees), `brain.js`'s
`getActiveSessionId()` replacing the hardcoded session string `'main'` everywhere it
appeared (including the monitor 'act' trigger and attachment prep in `server.js`), and
a new Chat History screen (search/rename/pin/archive/delete, a New Chat button in the
header). One real bug found in scratch testing: an object-literal property-evaluation-
order bug in the `GET /api/conversations` route — `chatStore.listConversations()` was
evaluated before `getActiveSessionId()` got a chance to lazily create the very first
conversation on a fresh install, so a brand-new install showed an empty list next to a
real, valid `activeId`. Verified on an isolated scratch server (separate port,
`JARVIS_DATA_DIR`, the user's real `.env` read-only for later Gemini calls): full CRUD
over curl, and — the thing code-reading can't prove — an actual kill-and-relaunch of
the scratch process confirming the conversation, its pin state, and the active id all
survived. User tested and confirmed working ("it works") before Stage 2 began.

**Stage 2** — `server/memory/memory-store.js` (the full Memory Manager: CRUD, version
history, merge, archive/restore, the candidate queue — no UI page, by design),
`memory-policy.js` (`decide()`, the single seam that always returns
`'require-approval'` in this build), `memory-review.js` (the checkpoint/extraction
engine — one batched `askModel` call per checkpoint, never per turn). Four checkpoint
kinds: new chat, Jarvis reopening (checked once, at server startup), a scheduled
`prompt`-type task finishing, and the model itself calling a new `checkpoint_memories`
tool when it senses a topic wrapped up — every one of them fire-and-forget, since a
checkpoint must never delay the user's own reply. Conflict detection rides in the same
extraction call as candidate discovery, at no extra cost. Conversations and memories
never cascade into each other, enforced by the schema itself (`memory_candidates`
cascades on its conversation FK; `memories` has no FK to conversations at all) rather
than by any code remembering not to. `data/profile.json` was migrated once, inside
`db.js`'s own migration step (so it can only ever run once), into a fixed `About You`
Memory category; `profile.js` became a thin adapter so `server.js`'s routes and
`public/screens/profile.js` needed zero changes. Five new skills
(`remember_about_me` rewired rather than replaced — one "remember" tool total —
`forget_something`, `update_memory`, `review_memories`, `checkpoint_memories`), plus an
in-chat review card in `public/app.js` (Approve/Edit/Reject, a real three-way choice —
Update/Keep both/Discard — for conflicts).

Verified live, against the user's real Gemini key, on a second isolated scratch
instance (trimmed `connections.json`/`models.json` copied in alongside the real
`.env`): the `profile.json` migration (correct text, order, and a real "migrated"
version-history row per entry); a full `remember_about_me` confirm → save round trip
through the actual model; a direct extraction test correctly finding 3 candidates from
a test excerpt, including correctly flagging a real conflict against an existing
memory; all three conflict-resolution paths (`update`/`keep_both`/`discard`); the
checkpoint pointer correctly advancing so a repeated checkpoint on unchanged content
costs nothing; the review card exercised for real through `agent-browser` (a live
screenshot round-trip, the Edit-then-save flow, Discard); and a second restart-survival
test. Two real bugs found and fixed along the way: a stray backtick inside a SQL
comment that was itself inside a JS template literal in `db.js` — it silently closed
the string early and corrupted the parse of everything after it in the file, tracked
down by isolating and bisecting the migration function in a scratch file since the
reported error line didn't match the real cause; and the review card's "N suggestions"
header staying stale after resolving one of several candidates (found live via
`agent-browser`, fixed, reverified).

A follow-on documentation pass brought `CLAUDE.md` in line with what was actually
built: new "Chat Persistence" and "Memory" sections in root `CLAUDE.md`, a new
`server/memory/CLAUDE.md`, an extended meta-skills list in `server/skills/CLAUDE.md`,
and — caught while re-reading the code for this pass — a fix to `db.js`'s own header
comment, which had gone stale mid-session (it still said Memory "deliberately stays
plain JSON" after the SQLite-for-both decision superseded that).

**A recurring observation, not caused by this session:** the user's real, already-running
Jarvis instance restarted on its own twice during this work (confirmed both times via
`netstat`/`Get-Process` that no kill this session issued reached port 3000), each time
picking up a snapshot of still-in-progress code from disk — most likely a second
concurrent session or the user restarting it themselves. Each time, this was
surfaced to the user directly rather than silently worked around. Now a permanent
Gotcha in root `CLAUDE.md` so a future session recognizes the symptom (a route that's
clearly in the file on disk 404s against the real port) rather than assuming its own
edit broke something.

---

# Original file, unedited below this line

**Four independent feature tracks, worked on by different sessions, sometimes
concurrently.** The topmost section below — **composer/conversation-panel
layout fixes + rail-width narrowing** — is now the most recent chronologically,
the latest entry in the voice/conversation UI track (picking up from "floating
conversation rail + orb-stability fix" further down that same track). Next,
**App Control connector UI rebuilt to match Claude + a general auto-updating
icon system** is its own track (App Control/Connectors only, picking up where
the "rebuilt Connectors to match Claude's" and "connector/permissions full
rebuild" sessions further down left off). Next,
**Skills system rebuilt from scratch, then three real follow-up bugs** is its
own track (Skills only). Everything from "Earlier
session — connector/permissions full rebuild..." onward is a fourth,
separate track (App Control/Skills/Monitoring/Sandbox) that finished earlier
— **its Skills-specific claims below are now superseded**, see the
correction note right after this paragraph; the Connectors/Monitoring/
Sandbox parts of that track are still accurate (and are exactly what the
topmost session further built on).

**⚠ The "Stage 4" paragraph immediately below is stale for Skills
specifically.** It describes Skills as installable "from a bundled
directory or a pasted link" — that entire mechanism (the bundled catalog,
`server/skill-catalog.json`, link/URL install) was torn down and rebuilt
from scratch in the topmost session above, on the user's explicit
instruction, after the catalog was found offering Jarvis's own native
abilities as installable skills. Read the topmost section first; treat this
paragraph as historical record for Connectors/Monitoring/Sandbox only.

**App Control/Skills/Monitoring/Sandbox track status (as of when that track
finished — see the correction above for what's since changed): ALL FOUR
planned stages are done.** In order, across one long session (the section
below covers all of it): a full ground-up rebuild of
the connector catalog + permissions system (3 rounds, ending in "tear it
down and rebuild against the original prompt text, keep only the `+ Add`
button's shape"), a full rebuild of the MCP mechanism itself (2 passes, after
the user caught that the first "full rebuild" had left MCP's own code as
legacy), **Stage 3** (desktop control gets close/restore/arrange window +
auto-cleanup of its own scratch windows + a general Start-Menu app-launch
fallback + connector tools usable mid-control-session, plus a whole new
monitoring system — "watch for X, then act" — with its own amber status
bar), and **Stage 4** (an isolated code-execution sandbox, plus ~~Skills can
now be installed from a bundled directory or a pasted link and updated in
place~~ — see correction above, with a one-time-approval mechanism for
Skills that ship their own scripts, which is still accurate). Two honest,
flagged gaps going in: the strong (WSL) sandbox
backend could not be verified against a real WSL install (none on this
machine) and a third possible backend (Windows Sandbox) was deliberately
left undetected-but-unbuilt rather than shipped untested — everything else
was verified live, including a real install from the actual public
`anthropics/skills` GitHub repository. Full technical detail lives in
CLAUDE.md's "App Control connectors" and "In-progress: App Control / Skills
/ Monitoring / Sandbox upgrade" sections — this file is the session-by-
session narrative; CLAUDE.md is the reference (and CLAUDE.md's own Skills
section is fully up to date with the rebuild, unlike this stale paragraph).

**⚠ Two Claude sessions have repeatedly worked in this repo at the same
time** in earlier sessions (not confirmed during the most recent one, but
don't assume it can't happen again). If anything looks like it changed
underneath you, that's why — see the last gotcha in CLAUDE.md for the
working rules that held up.

Sections, most recent first: **the composer/conversation-panel layout-fix
session** (two real, distinct bugs in the Composer/Conversation Panel
relationship — the composer's growth pushing its own Send button off-panel,
then a second bug where the control row visually overlapped multi-line typed
text — plus a user-requested rail-width narrowing; latest entry in the
voice/conversation UI track, most recent chronologically), **the App Control connector-UI session**
(restyled the connector list/detail pages against real Claude Desktop
reference screenshots, rebuilt the per-tool permission control into a real
4-state dropdown, then built a general auto-updating icon system from
scratch after the user rejected hardcoding more per-service SVGs — its own
track, most recent chronologically), **the Skills-rebuild session** (full from-scratch
rebuild against Claude's real interface, then three real follow-up bugs — upload
format parity, a real drag-and-drop bug, and heading-contrast/size in the markdown
renderer — its own track, run after everything else below finished), **the
attachments session** (composer redesigned
to real tiles/thumbnails, and Jarvis can now actually read the ordinary files
a person has — Word/Excel/PowerPoint with tables and structure intact,
embedded pictures, chart data, formulas, any code/config file, and a
whole-file spreadsheet analysis path via the sandbox — a different feature
track from everything below, run concurrently with the Content Analysis
rebuild noted next), **the floating-rail / orb-stability session**
(latest entry in the voice/conversation UI track — the conversation panel
un-boxed and moved to float over the right edge, out of layout flow, plus a
fix for the composer resizing the orb while typing), **the voice/
conversation UI overhaul session** (orb, composer, dictation, two rounds of
voice-pipeline regression fixes, a CSS wrap fix — a different feature track
from everything below, run concurrently with some of it), **the App Control/
Skills/Monitoring/Sandbox session** (the full rebuild saga + Stages 3 and 4 — everything in that plan
is now built), **the session that added connector detail pages, tool
permissions, guided setup** (superseded by the full rebuild, kept for
history), **the session that removed native-ability settings from the
interface**, **the session that rebuilt Connectors to match Claude's**,
**the session that rebuilt Content Analysis + Planning Partner from a
refined spec** (deleted and rebuilt both from the ground up — not tested
against a real model yet), **the session that folded Planning + Content
Analysis into the conversation** (now superseded), **the session that built
them as pages** (superseded earlier still), **App Control Stages 1–3**
(superseded), **model discovery / task-popup rebuild**, then **the earliest
session**.

---

## Most recent session — composer/conversation-panel layout fixes + rail-width narrowing

Latest entry in the voice/conversation UI track (picks up from "floating
conversation rail + orb-stability fix" further down this same track). The user
came in frustrated — this exact area had reportedly been "fixed" several times
before without the underlying problem actually going away — so this session
worked from **live measurement against the real running server** (port 3000,
`agent-browser`, read-only, never restarted — the user was actively using it)
rather than from reading the CSS and reasoning about it, for both bugs below.
Plan file: `C:\Users\HP\.claude\plans\fix-the-existing-conversation-iterative-tiger.md`
— written twice in sequence (overwritten between the two bugs, per the "different
task, start fresh" plan-mode rule), so it currently holds only the second plan's
text; this section is the durable record of both.

**Bug 1 — the Composer's own growth pushed its Send button outside the panel.**
`#composer-dock` was a single scroll container wrapping BOTH the attachment
grid and the control row, capped at a fixed viewport fraction
(`calc(17.5vh - 24px)`) unrelated to the space actually available in the rail.
Once content exceeded that cap, the wrapper's own `overflow-y:auto` clipped
it — and the control row (paperclip/mic/Send) was the last thing inside, so it
was what disappeared. Measured live at 1366×768: with one attachment, Send sat
45px below the visible panel edge; with a long message + 2 files, 322px below
— completely unreachable. Root cause: the cap lived on the WRAPPER instead of
on the two regions that actually grow (the attachment grid, the text box), so
it had no way to know how much of its own content was the disposable part
(attachments/text, safe to clip) versus the part that must never be clipped
(the control row) — a distinction this exact area had reportedly been "fixed"
several times before without ever actually correcting.

Fix: moved the cap off the wrapper and onto the two regions that actually
grow. Two new tokens in `:root` — `--composer-chips-max: clamp(110px, 16vh,
214px)` and `--composer-text-max: clamp(39px, 14vh, 180px)` (halved for the
≤1150px stacked layout's shorter rail, in that media query) — applied
directly to `.attachment-chips` and `#text-input` as their own
`max-height` + `overflow-y:auto`. `#composer-dock` itself lost its cap and
scroll entirely; with both children individually bounded, its total height is
bounded by construction and never needs to clip anything. `app.js`'s
`autoGrowTextarea()` now reads `#text-input`'s own `max-height` from the
computed style (`getComputedStyle(el).maxHeight`) rather than writing an
unbounded inline height, so style.css stays the one source of truth for the cap
— the earlier version hardcoded 200px separately from the CSS cap, which is
exactly how the two had drifted apart before.

Verified: 20 cases (4 viewports × 5 content states, up to 6 attachments + a
long message) all show the composer's bottom edge pinned to the rail's bottom
edge (growth is upward only), the composer never clipping its own content,
and Send/mic/paperclip always fully inside the rail on one row in the right
order.

**Bug 2 — the control row visually overlapped multi-line typed text.** A
second, separate defect the user caught right after bug 1 shipped, using a
JSON reference spec of the intended layout (`bottom_control_row`: attach far
left, mic immediately left of Send, Send far right, positioned underneath the
composer's content — not sharing a line with it). `#text-form` was one flex
row with all four items as siblings (attach, `#text-input`, mic, Send) and
`align-items:flex-end`. Fine when the message was short, but as `#text-input`
grew taller (by design, from bug 1's fix), the buttons stayed pinned to the
row's bottom edge — the same vertical band the last 1-2 lines of typed text
occupy. Screenshotted proof: with 6 lines typed, the paperclip and mic icons
rendered directly over "line 4"/"line 5" of the message, not below the text
block at all.

Fix, CSS-only, no DOM changes (kept exactly to the user's explicit "do not
create separate vertical containers for these buttons" — all four elements
stay direct siblings in the same `<form>`; only how that one row *wraps*
changes): `#text-form` gained `flex-wrap:wrap`; `#text-input` gained
`order:-1` + `flex:1 1 100%` (forces it alone onto the first wrapped line,
regardless of DOM order); `#attach-button` gained `margin-right:auto` (pushes
mic+Send to the far right on the second line). Chosen over
`margin-left:auto` on the mic button specifically because that button is
conditionally `.hidden` when dictation isn't supported — an auto margin on a
non-rendered element wouldn't apply, silently breaking Send's right-alignment
in that case. Trade-off stated plainly to the user rather than hidden: the
control row is now a permanent second line (not just a wrap-triggered
fallback), so the composer's minimum height rose from 60px to 104px
(+44px) even when empty — this is what "underneath the composer content, on
the same horizontal row," described unconditionally in both the JSON and the
user's ASCII diagram, actually requires.

Verified: same 20-case matrix, all showing the control row's top always ≥ the
text input's bottom (zero overlap in every case, not just the worst one) and
bug 1's invariants still intact. Screenshots of the empty state and a 6-line
state both match the requested `[ Upload ] ... [ Record Mic ] [ Send ]`
layout exactly.

**Rail width narrowed on direct request, twice in the same session**: 640px →
540px → 500px, each a one-line change to `--rail-width` in `:root`
(`public/style.css`), each confirmed live via `agent-browser` (rail/composer
`getBoundingClientRect()` widths measured directly, not assumed from the CSS
edit alone). `#stage`/the orb are already decoupled from this token (see the
"floating conversation rail" section below), so neither change touched
anything else.

Files touched: `public/style.css` (`:root` tokens, `#composer-dock`,
`.attachment-chips`, `#text-input`, `#text-form`, `#attach-button`, the
≤1150px media query, `--rail-width`), `public/app.js`
(`autoGrowTextarea()`). No HTML changes either bug.

---

## Most recent session — App Control connector UI rebuilt to match Claude, then a general auto-updating icon system

Two rounds, the second substantially larger than the first, both driven by
the user comparing Jarvis's screen directly against real Claude Desktop
screenshots rather than a description.

### Round 1 — bug fixes, then a screenshot-driven visual rebuild

Started as a bug-fix pass on the existing App Control screen (built in the
"connector/permissions full rebuild" track below): the header CSS was
absolutely-positioned with no scoping, so it floated over and ate clicks on
the first element of every section screen (`+ Add`, `← Back`) — traced with
a live DOM check, fixed by scoping the rule to `#app-screen > header`
specifically. Also fixed: raw MCP tool descriptions (thousands of characters
of internal, model-facing documentation) were being dumped straight onto the
connector detail page — removed from the UI-facing payload entirely
(`server.js`'s `toolsForConnector()`), left untouched for the model-facing
tool list. The old permission control was rebuilt from a plain checkbox into
a three-icon Always-allow/Ask/Deny switch (`_ui.js`'s `segmented()`-based
`permissionSwitch()` in `_connector-detail.js`).

Then the user sent two real Claude Desktop screenshots and asked for a
structural rebuild against them, not another patch — **three explicit scope
decisions, all still in force**: keep Jarvis's MCP/API/CLI tabs rather than
merging into one Claude-style list (a real backend distinction, not just a
label); Disconnect stays red, matching Jarvis's own destructive-button
convention, not Claude's neutral gray; the separate "Browse connectors"
catalog modal was restyled to match too (a "Popular" card strip + a plain
table, `app-control.js`). The permission control was rebuilt again into a
real 4-state dropdown (`_ui.js`'s new `dropdownControl()`) — Always allow /
Needs approval / Blocked / **Custom**, where Custom is a computed,
display-only aggregate (shown when a group's tools don't all agree, never a
settable bulk value) rather than tracked state, which is what makes the
dropdown's own label always honest rather than static. A second live
regression from this rebuild — the new dropdown lost its right-alignment
CSS class the old plain button had — was caught from a follow-up screenshot
and fixed (`.tool-group-bulk` re-applied to `dropdownControl()`'s wrapper).

### Round 2 — the icon system, built from scratch after a direct rejection of hardcoding

The rebuild's real per-service SVGs (5 hand-drawn marks for Notion/GitHub/
Slack/Google Drive/Gmail) drew a direct objection: custom connectors
(Composio, Lovable) still showed a generic 🔌, and the user explicitly
rejected "patch in one more hardcoded icon every time I report one" —
asking for a general, automatic mechanism instead, the same way Claude
shows a real logo for practically any service with no manual curation.

**`server/connectors/icon-resolver.js`, new.** Resolution order for a
connector's icon: (1) an MCP server's own declared icon from its
`initialize` response's `serverInfo.icons` (a fairly new, optional part of
the MCP spec — `mcp-remote-client.js`'s `initialize()` now caches whatever
`serverInfo` it receives, read via the new `getServerInfo()`); (2) a real
favicon fetched for the connector's own domain, via DuckDuckGo's public
icon endpoint (`icons.duckduckgo.com/ip3/{domain}.ico`, no key needed,
verified live against real domains throughout — not assumed), retried
against the domain's apex if the exact host has none (API/MCP subdomains
often don't carry their own favicon even when the marketing root does);
(3) the existing generic glyph. Fetched server-side and cached as a `data:`
URI (never client-side — keeps the browser from pinging a third party on
every render), re-checked every 30 days so a real rebrand eventually gets
picked up automatically rather than being a one-time snapshot.

**A real, live discovery mid-build**: Gmail and Google Drive's own domains
(`gmail.com`, `drive.google.com`, even `mail.google.com`) all resolve to the
exact same generic Google "G" favicon, not a per-product icon — verified
directly, not assumed. Chasing a better source turned up that Google Drive's
real logo **genuinely changed on May 19, 2026** (a gradient mark, nothing
like the older flat triangle) — live proof of exactly the staleness problem
the user was pushing back on. Fixed with `CATALOG_ICON_URLS`, a small curated
map of direct, verified asset URLs (Wikimedia Commons) for just these two
services, fetched through the exact same cache-and-refresh mechanism as
everything else — not hand-traced SVG path data.

**Two real "gated on the wrong thing" bugs, found only by checking the
user's actual live data after they reported nothing had changed** (not by
re-reading the code — both looked correct on paper):
1. Catalog icons were resolved only onto an existing connector *record* —
   meaning anything never clicked "Connect" on (still sitting in the Browse
   directory) never got one. Fixed by decoupling entirely: catalog icons now
   resolve into their own independent cache (`data/catalog-icons.json`,
   `resolveCatalogIcons()`/`getCatalogIcon()`), regardless of connection
   status — a catalog entry's domain is known from `catalog.json` alone.
2. Custom connector icons were resolved only for connectors already
   confirmed `status.state === 'working'` — so a connector added but never
   successfully connected (the user's real Lovable, `status: "untested"`)
   silently never got one either, indefinitely. The favicon-by-domain path
   never actually needed a live connection at all (only the address the user
   already typed in); only the MCP-declared-icon check genuinely does.
   `backfillConnectorIcons()`'s candidate filter dropped the status
   requirement; the real handshake call inside the loop stays gated on
   `'working'` so an untested connector is never freshly (and possibly
   unauthenticated-ly) connected-to purely to fetch an icon.

**A real trust/debugging lesson worth repeating**: after round 2 first
shipped, the user reported nothing had changed after a restart. Every
internal error path in `icon-resolver.js` was a bare, silent `catch {}` —
so there was no way to tell a real failure from "hasn't run yet" from the
outside. Added real `console.warn`/`console.log` at every failure point and
around both startup calls (`backfillConnectorIcons().catch(...)`,
`resolveCatalogIcons().catch(...)` in `server.js`) before investigating
further — the fix that actually mattered next turned out to be finding the
Lovable status-gate bug above, not a fetch failure, but the logging is what
makes the *next* real failure (if any) actually diagnosable instead of a
repeat of this same back-and-forth.

**Bonus fix, same session, unrelated root cause**: Chrome was autofilling a
saved credential into the OAuth guided-setup Client ID/Secret fields (and,
by the same shared code path, a custom API connector's key field and a
model connection's key field) — Chrome ignores `autocomplete="off"`
specifically on `type="password"` inputs. Fixed once in the shared
`fieldInput()` helper (`_helpers.js`) with `autocomplete="new-password"`
(the value Chrome actually respects for this), covering all three current
fields and any future password field built through the same helper.

**Status**: all of the above confirmed working by the user after a real
restart and live use — the icon system, the dropdown fixes, and the
autofill fix. One thread was still open at session's end: the user reported
Lovable's own detail page kept showing an old icon; traced live in a fresh
browser tab and it rendered correctly, and the underlying data/API were
independently confirmed correct — most likely a stale tab on the user's
end (single-page apps don't refetch on their own once already sitting on a
page), not a code bug, but not independently re-confirmed by the user as of
this write-up. Worth checking first if this resurfaces.

## Earlier session — Skills system rebuilt from scratch, then three real follow-up bugs

**Supersedes the Skills-related claims in "connector/permissions full rebuild...
Stage 4" below** (the "Skills can now be installed from three places [...] or a
pasted link" line, and the bundled catalog it describes) — that entire mechanism
was torn down and rebuilt in this session, not extended. See CLAUDE.md's Skills
section for the permanent design rule this session added.

### Why: the native-ability leak, confirmed a third time

The Skills screen's "Browse skills" catalog was offering Jarvis's own native
abilities (e.g. `webapp-testing`, duplicating the always-on browser connector) as
installable third-party skills — the same class of bug as two earlier, separately-
fixed instances (an early create-form draft, and the Morning Briefing's old "Live
info sources" picker). The user asked for a full rebuild against Claude's real
interface, not another patch.

### What changed, structurally

- **`server/skills-fs.js` deleted**, replaced by `server/skills/store/
  {skill-files,skill-zip}.js`, built against Claude's real `SKILL.md` spec (verified
  via Anthropic's docs and real screenshots the user provided) — no separate
  `title` field (the `name` slug is what displays, matching Claude), no per-skill
  settings form (Claude's skills have none).
- **No marketplace, no install-from-link** — `server/skill-catalog.json` deleted.
  Verified no Claude surface (claude.ai, API, Claude Code) installs a skill from a
  URL; Jarvis doesn't either now.
- **The leak is now structurally impossible, not just remembered**: every
  skill/tool entry carries `kind: 'builtin'|'skill'|'connector'`, and
  `listUserSkills()` — which can only ever read `data/skills/` folders — is the
  *only* function any Skills UI may call.
- New `public/screens/skills.js` (list) + `_skill-detail.js` (detail page: ⋮ menu —
  Try in chat / Edit / Edit with Jarvis / Replace / Download / Uninstall) rebuilt to
  match real Claude screenshots closely: same `+ Add` menu (Upload a skill / Write
  skill instructions / Create with Jarvis), same list columns, same detail-page
  layout. "Create with Jarvis" is a composer pre-fill into ordinary chat (`jarvis:
  start-skill-chat` window event), not a separate screen — mirrors Claude's own
  "Create with Claude" exactly. New `server/skills/create_skill.js` (upserts —
  creates or updates by name) is what actually saves it, reachable from that button
  or from plain conversation.
- **Morning Briefing's "Live info sources" card removed entirely** — weather/
  headlines are fixed native abilities with no UI now (same precedent as Desktop
  control/Browser/Files having none), set conversationally via
  `configure_briefing.js`. `briefing-config.js` reverted from a `sources[]` shape
  back to plain `weatherPlace`/`headlines` fields (with upgrade logic for both
  older saved-config generations it might still see).
- CLAUDE.md gained a **permanent, broadened rule**: native abilities can never
  appear as a Skill on any screen, not just "the Skills screen" — the old wording's
  narrowness is exactly why the Briefing-page instance went unnoticed before.

Full narrative and every verified test: `skills-system-rebuild.md` memory file (the
project's own persistent-memory store, not this handoff — that file also has the
three follow-up fixes below in more technical detail).

### Follow-up 1 — upload only accepted `.zip`, a real gap

A real Claude "Upload skill" screenshot showed Claude actually accepts `.zip`,
`.skill` (confirmed via two independent sources to be nothing but a renamed
`.zip`), and a bare `.md` file — plus a drag-and-drop zone and visible
file-requirement text Jarvis's dialog didn't have. Fixed: `skill-zip.js` now sniffs
the ZIP magic-byte signature rather than trusting a claimed filename (a raw-body
upload never reliably carries one anyway), dispatching to either the existing
unpack path or a new `installFromMarkdown()`/`replaceSkillMarkdown()` pair.
Wrapper-folder tolerance deepened from one hardcoded level to a bounded walk — the
user's own real test zip (a GitHub repo export, re-zipped by hand) had the "zipped
the folder instead of its contents" mistake nested *twice*. `skills.js`'s Upload
dialog now has a real drag-and-drop zone + the same two requirement lines Claude's
own dialog shows. Verified via six real upload cases on a scratch server (zip,
`.skill`-renamed zip, valid `.md`, invalid `.md`, doubly-nested zip, stray binary) —
all behaved correctly.

### Follow-up 2 — drag-and-drop *looked* like it worked but didn't

User-reported, with a screenshot: dropping a real file (a genuine Claude Code skill,
`~/.claude/skills/council/SKILL.md`) showed the filename in the dropzone, but
clicking Upload still said "Choose a file first." Root cause: the dropzone's
`drop` handler updated the on-screen label but never touched `fileInput.files` —
nothing does, a browser only populates a file input's own `.files` from ITS OWN
picker, never from a drop on a different element — while `getFile()` only ever read
`fileInput.files`. Fixed by tracking the chosen `File` in a plain variable set by
`choose()` regardless of which path fired it. **Caught only by actually dropping a
real file through the real browser UI** — the first round's testing verified the
server thoroughly via `curl` and only screenshotted the empty dropzone, never
exercising the real event wiring. Verified this time with a genuine reproduction:
`agent-browser eval` building a real `File` + `DataTransfer` and dispatching an
actual `drop` event with the user's exact `council/SKILL.md` content — installed
correctly afterward.

### Follow-up 3 — skill files "render as one long block of plain text"

Investigated by reading `_markdown.js`/`style.css` first — found nothing wrong;
real `<h3>`-`<h6>`, real lists, real inline `<code>`, real code blocks, all
correctly generated and styled on paper. The actual bug only surfaced by directly
comparing two screenshots of the *same file* (`council`) open in both Claude and
Jarvis: `.md h5, .md h6` were muted gray — and a `### numbered sub-step` (very
common in a workflow-shaped skill, e.g. "1. Frame the council question") lands
there, since the renderer shifts headings down two levels so a skill's own heading
never competes with the screen's `<h2>`. Claude keeps every heading level
full-contrast. First fix (drop the muted color, nudge h3 from 1.05rem to 1.15rem)
was too timid — user reported "still the same thing" against a live screenshot even
though the color half had genuinely landed. Second pass sized more decisively
against Claude's real proportions: h3 1.05rem → **1.5rem** + explicit
`font-weight: 700`, h4 → 1.15rem, h5/h6 → 1.05rem. Re-verified both times by
re-uploading the user's real `council/SKILL.md` to a scratch server and
screenshotting the same heading.

### Still open

- **Task A, the user's own next-requested piece — not started**: a dedicated
  verification subagent proving Jarvis actually *invokes* a skill during a real
  conversation (not just that upload succeeds), across four cases — single-skill
  trigger, correct pick among several, an ambiguous/overlapping case, and a
  no-match negative case — each backed by independent evidence (logs/traces, not
  Jarvis's own claim), reported as a pass/fail table. User confirmed real API
  quota use is fine for this. Explicitly ordered *after* the formatting work above,
  which is now done.
- **The user's separate "Claude itself rejects a plain zip/folder" claim was never
  resolved** — screenshots of that specific error were requested twice and never
  arrived (a duplicate-path mistake sent the same Claude screenshot twice instead).
  Everything actually diagnosed and fixed above turned out to be genuine Jarvis-side
  bugs unrelated to that specific claim — it's still an open thread if picked back
  up.
- Every scratch-server test in this session ran on port 3277 with an isolated
  `JARVIS_DATA_DIR`; the user's real server (port 3000, restarted several times by
  the user or a concurrent session during this work, PID confirmed different each
  time) was checked before/after every round and never directly touched.

---

## Earlier session — attachments: composer redesign + real file support

Plan file: `C:\Users\HP\.claude\plans\attachments-the-current-shimmying-hippo.md`.
A separate feature track from everything below it, run concurrently with the
Content Analysis rebuild session — **while this session was working, that
other session renamed `server/analysis/` to `server/content/` underneath it;
see "What was found mid-session" below.**

### What the user asked, and what turned out to be true instead

Two complaints: the attached-file "chip" in the composer looked wrong (a
long text-only pill, no visual difference between a photo and a spreadsheet),
and "the file picker is restricting file types."

The second one was investigated and reported back before any code changed:
**the picker restricts nothing.** `public/index.html` has no `accept`
attribute at all (deliberate, with its own comment saying so), and there's
no client-side validation of any kind — any file uploads successfully. The
restriction happens entirely *after* upload, in the fixed extension lists
`media.js`/`attachments.js` used to decide what to do with a file once it
arrived. A separate, real defect was found in the same pass: `.json` files
could not be uploaded at all (see below).

During interviewing (`AskUserQuestion`), the user pushed back twice on my
own answers and was right both times: I'd claimed embedded pictures and
chart data inside Word/Excel/PowerPoint files were out of reach — wrong, both
are stored directly in the file and are extractable; and I'd defaulted large
spreadsheets to silent truncation — the user pointed out the project already
has a sandbox built for exactly this ("does that mean Jarvis can process the
whole thing?") and asked for real whole-file analysis instead. Both went into
the plan as a direct result of the user asking a second time rather than
accepting the first answer.

### What changed

**Composer tiles** (`public/style.css`, `public/app.js`) — the pill replaced
with a 96px square card, matching a screenshot the user provided of Claude's
own composer: a real thumbnail for images, filename + uppercase extension
badge for everything else, an always-visible circular × top-left, wraps to
two rows then scrolls. `renderAttachmentChips()` now actually reads
`att.previewUrl` (previously built but never wired to the chip UI — it was
only ever used in the sent-message bubble).

**`server/documents/`** (new, 8 files) — real Word/Excel/PowerPoint reading,
hand-rolled with no new dependency (this project deliberately runs on four
npm packages): `zip.js` (a minimal ZIP reader over Node's built-in
`zlib.inflateRawSync` — a `.docx`/`.xlsx`/`.pptx` is just a ZIP of XML),
`xml.js` (a small tree-building XML parser, not a full spec implementation),
then one reader per format (`docx.js`/`xlsx.js`/`pptx.js`) plus two shared
pieces (`media.js` for embedded-picture extraction, `charts.js` for chart
data), unified behind one seam (`index.js`'s `extractDocument()`). Output is
Markdown, which means **every model can read an Office document now, not
just Gemini** — no capability gate. `xlsx.js` places every cell by its own
`r="C5"` reference rather than by iteration order, specifically because a
real workbook's blank cells are usually absent from the XML entirely; walking
cells in document order would silently shift every later column left. Also
detects and converts Excel date-serial numbers (`41640` → `2014-01-01`) via
`styles.xml`, which an earlier pass had missed entirely.

**`server/skills/analyze_spreadsheet.js`** (new) — for a workbook too big to
inline: reads the sheet's shape (columns, row count, a small sample), asks a
model to write a short Node script, runs it in the existing sandbox
(`server/sandbox/runner.js`) against the REAL, full file (as CSV — the
sandbox has no spreadsheet library), and returns the answer plus the script
itself so the user can see how the number was reached. One retry on a
failing script, not a loop. Deliberately not `confirm: 'always'` — the script
is generated from the user's own question against a file they just attached,
with no host folder access, and re-confirming every follow-up question about
one spreadsheet would make the tool unusable.

**Other file-support additions**: a generic byte-sniffing text reader
(`attachments.js`) so any code/config file (`.js`, `.py`, `.ini`, ...) is
read correctly without the extension list growing forever; browser-side
image normalization (`app.js`) — a picture is re-encoded to JPEG via canvas
before upload, fixing oversized phone photos and converting
AVIF/WebP/BMP/ICO/SVG, with HEIC/TIFF reported plainly as unsupported
(Chrome can't decode either); more video/audio extensions recognised
(`.3gp`, `.wma`, `.amr`, `.mts`, `.m4b`).

**The `.json` upload bug, fixed** (`server.js`) — the global
`express.json()` body parser was mounted before the `/api/uploads` route, so
a browser labelling a `.json` file's upload as `Content-Type: application/json`
had its body silently parsed and consumed before the route's own
`express.raw()` ever saw it — `saveUpload()` got a parsed object instead of
a Buffer and failed with "That upload was empty." Fixed by mounting
`express.raw()` for that one path before the global JSON parser.

### Two real bugs found only by testing against real, downloaded files — not by reading the code

No `.docx`/`.pptx`/chart-bearing file existed on this machine, so real
fixture files were downloaded from `python-docx`'s and `python-pptx`'s own
public GitHub test suites (`blk-paras-and-tables.docx`, `tbl-cell-access.docx`,
`shp-inline-shape-access.docx`, `cht-charts.pptx`) rather than testing the
reader against a file built by the same code that reads it.

1. **The embedded-image filter used file size as a proxy for "is this a
   bullet icon," and it was wrong.** The first version dropped any image
   under 8KB as presumed furniture. Against the real `shp-inline-shape-
   access.docx` fixture, two genuine, legitimate embedded photos (6.1KB and
   3.3KB — ordinary JPEG compression, not tiny) were silently dropped every
   time. Fixed by reading each format's real header bytes for actual pixel
   dimensions (PNG/GIF/BMP/JPEG, hand-parsed — a JPEG needs a marker-segment
   walk since it has no fixed offset) and filtering on physical size
   instead — a 16×16 icon is small regardless of compression; a real photo
   compressed to 3KB is still a real photo.
2. **Chart-data extraction was reading the XML tree at the wrong depth and
   silently returned an empty table every single time**, never an error.
   `<c:strCache>`/`<c:numCache>` are not direct children of `<c:tx>`/
   `<c:cat>`/`<c:val>` — they sit one level deeper, wrapped in a
   `<c:strRef>`/`<c:numRef>`. The direct-children-only lookup found nothing,
   every time, and nothing surfaced the failure — the chart section just
   rendered with headers and no rows. Found and fixed against the real
   `cht-charts.pptx` fixture; re-verified afterward that every series name,
   category, and value in the rendered table matches the source XML exactly
   (12 numbers checked by hand against the raw file).
3. A third, smaller bug: `pptx.js` never actually called the chart-extraction
   function that had already been written for it — `docx.js` called it,
   `pptx.js` didn't, so a chart-bearing PowerPoint file produced no chart
   section at all until this was added.

### What was found mid-session: a second, concurrent Claude session

Partway through, `server/analysis/` and `server/planning/` were found to no
longer exist on disk, while `server/attachments.js` (untouched by this
session at that point) still imported `./analysis/analyzer.js` — a file that
was no longer there. Investigated carefully before touching anything
(confirmed via a direct, native PowerShell listing, not just Bash, in case of
a path-quoting artifact) rather than assumed broken or "fixed" by guessing:
a second session was live-renaming `analysis/` → `content/` (and rebuilding
Content Analysis + Planning against a refined spec — see that session's own
entry above) at the same time. Left entirely untouched; where this session's
own edits intersected the same file (`attachments.js`, which both sessions
needed to touch), the other session's later rewrite of the video/audio
branch merged cleanly with this session's additions with no conflict,
confirmed by re-reading the file fresh immediately before every edit and by
a full live end-to-end test afterward (see Verified, below).

### Verified

Real files throughout, not hand-built fixtures — and a live scratch server,
never the user's real instance (port 3000, confirmed by PID before and after
every test):

- Every `documents/` module tested directly (`node --input-type=module -e`)
  against real files: the user's own real Excel workbook (~700 rows × 16
  columns, `Financial+Sample.xlsx`) for `xlsx.js` — column alignment,
  sparse-row handling, date detection, CSV/Markdown output all confirmed
  correct by hand; the downloaded `python-docx`/`python-pptx` fixtures above
  for `docx.js`/`pptx.js`/tables/hyperlinks/images/charts.
- **A full end-to-end run on a live scratch server** (real API key via
  `JARVIS_ENV_PATH` pointed at the real, read-only `.env`; scratch data dir;
  a separate port): attached the real 700-row workbook and a real code file
  together, asked Jarvis a question about both, and — after failing over
  through several models with real, unrelated problems (quota exhaustion, a
  retired model, network timeouts to Google, all visible in the server log
  and none originating from this session's code) — got back a correct answer
  describing both files accurately. Confirms the whole chain end to end:
  upload → classification → Office-document reading → generic text-sniffing
  → message composition → model turn.
- **The composer tiles, visually**, via `agent-browser` against the same
  scratch instance: attached an image, a `.docx`, a real `.xlsx`, and a code
  file together — real thumbnail rendered for the image, correct type badges
  for the rest, removal confirmed working, tiles wrapped cleanly.
- **The `.json` fix, live**: both a small `.json` file and one at 288KB
  (well past the old 100KB default limit) uploaded successfully against the
  fixed server; the failure was not separately reproduced against the old
  code in this pass (the bug and its mechanism were already fully understood
  from reading `server.js` directly).
- Confirmed the real server on port 3000 was never touched or restarted at
  any point — same PID checked before and after every scratch-server test.

### Open items

1. **The user has not yet tested this in their own browser** — a plain-
   English walkthrough was written directly into the plan file (6 numbered
   things to try: the new tile look, a real Office document, a code file, a
   big phone photo, a big spreadsheet, and the `.json` fix) for them to run
   through themselves.
2. **Chart-data extraction is the least-verified single piece** — real, and
   now genuinely verified against one real chart fixture (12 values checked
   by hand), but only one chart layout has been tested. If a user's real
   file's chart doesn't extract cleanly, that's the first place to look.
3. HEIC/TIFF photos, exact visual/page-layout rendering, hand-rolled PDF
   text extraction, and OCR of scanned documents are all deliberate
   non-goals, not gaps — see the plan file's "Deliberate non-goals" section
   for the reasoning behind each.
4. The second session's `analysis/` → `content/` rename is that session's
   own work to document, not this one's — see its entry above/below for
   status.

---

## Earlier session — floating conversation rail + orb-stability fix

Plan file: `C:\Users\HP\.claude\plans\hey-claude-i-ve-tested-cuddly-feigenbaum.md`.
Continues the same conversation-panel evolution as Part 4 of the session
below (which had already turned it into a floating rail to fix a horizontal-
scrollbar bug) — this session is the next round on that same panel, done as
a separate conversation.

### What the user reported

The conversation panel was distorting the main interface: it reserved real
layout space as a flex column (`#conversation-rail { flex: 0 0 340px }`),
which pushed the orb and composer off-centre (~180px left of true centre).
It also looked like a boxed widget — background, border, rounded corners,
and a "CONVERSATION" header bar — when the user wanted it to read as part of
the main interface, just relocated to the right: no visible container, taller
(~60–70% of height instead of 50%/460px-capped), fixed-width, internally
scrolling only, never resizing the orb/composer.

Per this project's process (plan mode first), reported back the exact root
cause before writing any plan, then used `AskUserQuestion` to resolve three
open calls before finalizing: (1) reserve equal space on **both** sides of
the stage so the centred orb/composer can never slide under the floating
rail on narrower windows, rather than letting them overlap or narrowing the
rail; (2) remove the "CONVERSATION" header entirely, not just de-style it;
(3) also fix a separate, related defect the user hadn't explicitly asked
about but that undermined "the orb must stay stable" — typing a multi-line
message grew the composer and visibly shrank/shifted the orb, because
`#orb-stage` was `flex: 1` in the same flex column as the composer.

### What changed

`public/index.html` — deleted the `#conversation-panel` wrapper and
`.panel-head` header; `#conversation-rail` now holds only
`#conversation-scroll` directly.

`public/style.css` — `#conversation-rail` changed from a flex column
(`flex: 0 0 340px`) to `position: absolute`, floating over the right edge
with no background/border/radius, fixed 340px width, 65% height. `#stage`
gained symmetric left/right padding (`--rail-reserve`, 364px) so the orb and
composer stay exactly centred on the real interface and can never reach
under the rail. `#orb-stage` was pulled out of flow entirely
(`position: absolute`, height `calc(100% - var(--stage-bottom-reserve))`,
`pointer-events: none`) so its size no longer depends on the composer's flow
siblings — a growing composer can no longer shrink or shift the orb. The
composer's `max-height` dropped from 200px to 120px (also updated in
`autoGrowTextarea`, `public/app.js`) so it can't grow tall enough to reach
the orb even at its cap. The side-by-side breakpoint moved from 900px to
1150px, the real cutoff where the new side reserves stop leaving room for a
usable composer; the stacked fallback below that resets the rail to static/
in-flow and `#orb-stage` back to static.

**A real bug found only by live verification, not by reading the CSS**:
`#mic-hint` is a `<p>` with no margin reset, so the browser's default `1em`
top/bottom paragraph margin was stacking on top of `#stage`'s own flex
`gap`, silently costing ~28px beyond what `--stage-bottom-reserve` (200px,
first attempt) budgeted for. At a short window height, typing a maximally-
grown composer pushed the mic button up into the orb's now-fixed overlay
box — an actual visual collision, not the paper design. Found by measuring
live `getBoundingClientRect()`s after typing a long message, not by
eyeballing a screenshot. Fixed two ways together: zeroed `#mic-hint`'s
margin (the real fix — mixing UA paragraph margins with flex `gap` in the
same container was the root cause) and recalculated the reserve precisely
from live measurements (mic-button 64 + gap 12 + hint 18 + gap 12 + composer
at max-height 142 + stage's own padding-bottom 8 = 256px, `--stage-bottom-
reserve` set to 264px for an 8px buffer). Re-verified after the fix: 8px
clearance between the orb's box and the mic button at max composer growth.

### Verification

`agent-browser` against the user's real running instance (port 3000, never
restarted — `public/` is served statically, so a hard reload was enough).
Chrome extension (`claude-in-chrome`) wasn't connected this session, so
`agent-browser` was used throughout instead, per CLAUDE.md's fallback
guidance. Confirmed live: orb/composer centred and unmoved through 8+
injected messages and transcript overflow; the panel has zero visible
box/border/background at any point, including empty; internal scroll with a
top/bottom fade instead of a hard clip line; fixed 340px width that doesn't
change while typing; the mic-button/orb collision fix (above); the stacked
fallback below 1150px (full-width, in normal flow, no separate scroll
container issue). Screenshots sent to the user directly (wide desktop,
composer expanded, narrow stacked) — the user has not yet visually confirmed
in their own browser.

One incidental finding, flagged and left untouched as out of scope: sending
real test messages during verification surfaced a pre-existing backend
error unrelated to layout — a Gemini tool-calling replay 400
("Please ensure that function response turn comes immediately after a
function call turn") on at least two of the user's configured models. Not
investigated or fixed this session.

### Open items

1. User hasn't yet confirmed the new layout in their own browser — screenshots
   were sent but a live look-over is still worth asking for.
2. The Gemini tool-calling replay 400 noted above is real and unrelated to
   this fix — worth a separate debugging session if it recurs; see
   CLAUDE.md's `thought_signature` gotcha as a likely-related starting point
   (a tool-calling turn replayed without the model's own signature intact).

---

## Earlier session — voice/conversation UI overhaul (orb, composer, dictation) + three rounds of voice-pipeline fixes

Plan file: `C:\Users\HP\.claude\plans\the-current-jarvis-interface-linear-mango.md`
(overwritten, not appended, between rounds as scope narrowed each time the
user came back with a follow-up — the current file on disk only reflects the
*latest* round; earlier rounds' reasoning is preserved below since it's no
longer in the file itself). **A different feature track from the App
Control/Skills/Monitoring/Sandbox session below — this one is entirely
voice/UI, and the two were worked on concurrently by different sessions; see
the note further down about that.**

### Part 1 — the original ask: stop the page from stretching, modernize the composer, add dictation, add a 3D orb, investigate self-listening

The user reported the whole page scrolling vertically as the conversation
grew, and asked for: the conversation moved into a side panel that scrolls
internally instead of the page; a modern auto-growing composer with
attachments/mic/send all inside one shell; a separate dictation mic in the
composer (distinct from the voice-control mic under the orb); a real 3D orb
reacting to idle/listening/thinking/speaking; and an investigation (not yet a
fix) into why Jarvis sometimes "hears" its own voice as user input.

Root causes found and reported before any code changed: the page-stretch bug
was `.screen`'s `align-items:center` leaking into `#app-screen` (collapsing
every child to fit-content width) plus a missing `min-height:0` chain; the
self-listening bug was `pipeline-engine.js`'s `SpeechRecognition` never being
stopped/filtered while TTS played, compounded by the `echoCancellation`
constraint never actually reaching the recognizer (it only fed the mic-level
monitor — the Web Speech API opens its own separate, unprocessed capture).

Asked the user to choose the orb's tech stack before building (dependency-
free raw WebGL2 vs. vendored three.js) — they picked **vendored three.js**
(`public/vendor/three/`, pinned 0.185.1, pulled via `npm pack`), the
project's one deliberate front-end dependency.

Built: the two-column layout (`#app-body` → `#stage` centre + a conversation
panel beside it), the auto-growing composer (`#composer`, textarea-based,
Enter-to-send/Shift+Enter-newline), `public/dictation.js` (a second,
independent `SpeechRecognition` session for the composer mic, mutually
exclusive with voice control since Chrome only reliably runs one at a time),
`public/orb.js` (three.js scene: displaced `IcosahedronGeometry` + custom
vertex/fragment shaders, reactive to mic/output level), and the
self-listening fix (recognition suspended while Jarvis's audio plays + a
700ms tail for Chrome's ASR lag, energy-based barge-in replacing the old
text-based one).

**Two real bugs found and fixed live, not via code review**: (1) vendoring
only `three.module.min.js` and not its sibling `three.core.min.js` (the
entry point imports it) took the *entire app* down silently — nothing in
`app.js` runs until all its static imports resolve, and the failure gave no
useful console error, only a 503 visible in real network requests; (2)
`IcosahedronGeometry`'s second argument is subdivision *detail*, not a
resolution/segment count — passing `48` (a reasonable-looking "high
resolution" guess) tried to generate ~4^48 faces and hung a real browser tab
so completely that even trivial CDP `eval()` timed out, initially
misdiagnosed as an unrelated background-process death before the real cause
was found.

Verified live via `agent-browser` on scratch ports (never the user's real
instance): 40 injected messages + settings panel open → zero page scroll
(the core bug, fixed); textarea auto-grow; the muted-slash icon; the orb
rendering all four states.

### Part 2 — the user tested it live and reported the voice experience had regressed

Direct, important correction from the user: they had NOT been complaining
about the existing conversational feel — only asked for the mic to move
under the orb with mute/unmute — and the self-listening *fix*'s specific
implementation had gone further than needed and changed things that already
worked. Diagnosed and reported back precisely (no code changed until
confirmed) four real behavioral regressions, each traced to an exact line:

1. Recognition was suspended from the moment the user's utterance was sent
   (`_send()`/"thinking"), not from when Jarvis's audio actually started —
   killing the "talk freely during thinking" feel entirely, not just fixing
   the echo.
2. Barge-in gained an extra ~300ms "echo floor learning" warm-up before
   arming, roughly doubling how long it took to interrupt Jarvis.
3. `LiveEngine`'s mic upload was gated during Jarvis's own audio to prevent
   self-echo — but this silently disabled Gemini Live's *native* barge-in
   entirely, since Gemini's own server-side VAD can't detect an interruption
   in audio it was never sent.
4. `audio-player.js` gained a Web Audio `AnalyserNode` tap on every TTS
   `<audio>` element purely so the orb could react to Jarvis's real voice —
   routing playback through an extra graph node was unnecessary risk to
   actual audio output (autoplay-suspend timing) for a cosmetic effect.

Fixed all four, restoring original behavior while keeping the actual
self-listening fix intact (recognition suspend narrowed to exactly the
speaking window + tail; barge-in warm-up removed; `LiveEngine` mic streaming
restored to continuous; the audio tap removed, orb falls back to procedural
"speaking" motion). Same pass also found and fixed a second reported bug —
"Jarvis's voice sometimes stops halfway through a reply" — root-caused to a
**confirmed defect**: `MicLevelMonitor._speakingSince` (the barge-in "how
long has this been loud" timestamp) was never reset between replies, so a
stale value from the tail of one reply could make the barge-in gate on the
*next* reply pass almost instantly with no real sustained speech. Fixed with
a `reset()` at the start of each reply's barge-in window, plus three
supporting fixes: a bounded retry on failed `/api/tts` fetches (Gemini TTS's
low free-tier rate limit was a plausible cause of sentences silently
dropping near the end of longer replies), `speaker.end()` added to the
chat-stream error paths (previously only `'paused'`/`'done'` flushed/
recovered state, so a mid-stream error while speaking stranded playback),
and a watchdog timer in `browser-speaker.js` for a known Chrome bug where
`speechSynthesis` can silently drop an utterance with neither `onend` nor
`onerror` ever firing.

### Part 3 — two follow-up mute-button bugs, both found by the user actually using it, both fixed same-day

**Bug 1**: clicking Mute while Jarvis was thinking or speaking canceled the
reply outright — the mic button's click handler still called
`engine.stop()`/`engine.interrupt()`, which touch the in-flight turn. Fixed
with a real `setMuted(bool)`/`.muted` on both engines that touches *only*
microphone capture, composed cleanly with the self-listening suspend logic
via a `_shouldListen()` gate so the two never fight. Verified directly (not
just read) by constructing a `PipelineEngine` in a live browser, injecting a
fake `speaker`/`currentEventSource`, calling `setMuted(true)` mid-speech,
and confirming both objects were untouched afterward.

**Bug 2**, subtler, found immediately after: muting *right after finishing a
sentence but before Jarvis started responding* silently discarded the
turn — nothing was ever sent, no reply, conversation looked dead. Root
cause: `setMuted(true)` was clearing `pendingUtterance`/`silenceTimer`
(reasoning: "nothing spoken right before muting should be sendable later")
and `_maybeFinalize()` separately refused to send anything while muted —
both too blunt, since an utterance sitting in that buffer was always
something said *before* muting took effect (the recognizer's own
`this.muted` guard already prevents anything *new* from ever entering it
while muted), so it should still be sent. Fixed by having mute leave
already-captured content alone entirely; it now only ever blocks future
capture. Verified live: simulated a just-finished, not-yet-sent utterance,
muted, confirmed the text survived and would still finalize — and re-ran the
Bug-1 regression test to confirm no conflict between the two fixes.

### Part 4 — one CSS-only fix: the conversation panel's horizontal scrollbar

The panel (redesigned by a concurrent session partway through this work into
a floating rail, `#conversation-rail` — its position/structure changed more
than once underneath this session; always re-read fresh from disk before
editing rather than assumed) could grow a horizontal scrollbar when a
message contained a long unbroken token (URL, filename). Root cause:
`.bubble` had no `overflow-wrap`, and `.bubble`/`#transcript` (nested flex
containers) had no `min-width:0`, so a long token's min-content width could
exceed the panel's fixed 340px. Three-line fix (`overflow-wrap: break-word`
+ two `min-width: 0`s), verified by measuring `scrollWidth === clientWidth`
on the scroll container with a 220-character unbroken test string injected,
at both the desktop floating layout and the narrow/stacked mobile
breakpoint.

### A recurring theme this session, worth flagging for whoever picks this up next

**Another Claude session actively edited this same UI (`index.html`/
`style.css`/`app.js`) at least twice during this session** — confirmed via
the harness's own file-changed notices, not assumed. Each time, files were
re-read fresh before editing rather than trusting an in-memory copy; no
conflicts occurred because edits landed in different regions of the same
files. **If picking this up again, re-read `index.html`/`style.css`/
`app.js` before assuming their current shape** — the conversation panel
specifically has already been restructured more than once (two-column flex
layout → floating absolutely-positioned rail).

### Verification methodology (repeat this pattern)

Never touch the user's real instance (port 3000, confirmed by PID before/
after every test) — scratch ports via the `Bash` tool's own
`run_in_background: true` (plain shell `&`/`disown` was tried once and the
process died silently between tool calls, wasting real time misdiagnosing it
as a client-side hang — see CLAUDE.md's gotcha on this). `agent-browser` for
all UI verification, not `claude-in-chrome` (the latter's target was the
user's own long-running, heavily-loaded daily browser and repeatedly timed
out for unrelated reasons — `agent-browser`'s isolated Chromium instance was
reliable where it wasn't). For voice-engine internals that can't be
exercised without a real mic/model key, import the engine module fresh in a
live browser tab and call its methods directly with fake `speaker`/
`currentEventSource` objects to assert exactly what does and doesn't get
touched — this technique caught both mute-button bugs with certainty, not
inference.

### Open items

1. **Real end-to-end voice/audio confirmation is still the user's to do** —
   barge-in speed and feel, whether replies now reliably finish, whether
   mute genuinely never interrupts in live use. Everything above is verified
   as far as this environment allows (headless browser, no real mic/
   speakers); ask the user to confirm on their next real conversation.
2. Nothing else is known-broken as of the end of this session — the panel
   wrap fix was the last reported issue and is verified.

---

## Earlier session — connector/permissions full rebuild, MCP full rebuild, Stage 3, Stage 4

Plan file: `C:\Users\HP\.claude\plans\i-m-continuing-to-upgrade-glimmering-moon.md`
(kept up to date — the authoritative record of what's approved and what's
done; re-read it before assuming scope from git history alone).

This session started from the tail end of the connector-detail-pages work
below, then went through a hard escalation from the user before the real
new work (Stages 3–4) even began.

### Part 1 — the user's "rebuild everything" escalation, and what it actually meant

The user asked whether "Connector catalog" and "Connector/tool permissions"
(two specific paragraphs from their own original requirements prompt) had
really been addressed. Checking found the existing 3-way Always/Ask/Never
permission dropdown had conflated two things their own prompt kept
separate: a **standing permission** ("is Jarvis allowed to use this tool at
all") and **runtime confirmation** ("does it pause to ask right now,
regardless of standing permission"). Reporting that back honestly led to an
increasingly frustrated exchange — the user felt earlier answers were
dodging the real question — culminating in: *"rebuild everything... i don't
want to do this again."*

**What "rebuild everything" turned out to mean, learned the hard way**: not
"rebuild the most obviously related file" but *every* file that touches the
thing being rebuilt. The first rebuild pass covered `store.js`, `index.js`,
added `api-client.js`/`cli-client.js` as real new mechanisms, and rebuilt
the UI — but left `oauth.js`, `mcp-remote-client.js`, `mcp-client.js`, and
`get-catalog.js` (the actual MCP transport) as pre-existing code, since it
already worked. The user caught this immediately and explicitly: *"i asked
you to rebuild the mcp that was the mean issue i flag early."* Rebuilding
those four files as new code was still only half done — `server.js`'s MCP
routes, `app-control.js`'s Browse-connectors modal, and `_connector-
detail.js`'s connect-flow section were all still the old code underneath a
few cosmetic tweaks. A second pass rewrote all of those too. **The lesson,
now written into CLAUDE.md**: check every file that imports from or calls
into the thing being rebuilt, not just its own dedicated files.

Net result — genuinely new code, re-verified live against the user's real
Notion connector and real GitHub/Slack/Google Drive OAuth discovery
documents after every pass, no regression at any point:
- **Standing permission vs. runtime confirmation**, finally separated for
  real: a per-tool Allowed/Not-allowed toggle (standing) and an automatic,
  never-overridable `classifyToolRisk()` (confirmation) — see CLAUDE.md's
  "Per-tool permissions" section.
- **Three peer connector mechanisms** — MCP, API, CLI — each with its own
  App Control tab, `api-client.js`/`cli-client.js` genuinely new (OpenAPI/
  Swagger discovery for API, `<command> --help` + model-proposed subcommand
  review for CLI, never a raw shell string for either).
- **MCP rebuilt as an equal peer**, not legacy code the other two were built
  around — `oauth.js`, `mcp-remote-client.js`, `mcp-client.js`, `get-
  catalog.js` all new, all re-verified: two live DCR round trips against
  Notion, a live path-aware discovery check against GitHub, a real stdio +
  Streamable HTTP round trip against stub servers.
- Gmail added to the catalog (verified real endpoint + DCR support); Figma
  deliberately excluded (its real MCP server only accepts a small
  pre-approved client whitelist — verified against Figma's own docs, not
  guessed).

The user's response once this was done: *"let continue i would do more
adjustment later"* — i.e., proceed with Stage 3 and Stage 4 as originally
planned; further tweaks to what's built would come as separate asks, not a
reason to stall.

### Part 2 — Stage 3: desktop control upgrades + monitoring

Everything in the plan's Stage 3 shipped and was verified against a scratch
server (never the user's real instance, confirmed by PID before/after every
test):

- **`agent.ps1`** gained `close_window` (`WM_CLOSE`, never `Stop-Process` —
  see the Notepad shared-process disaster from an earlier session),
  `restore_window`, `arrange_window`. Live-tested against a real disposable
  Calculator window — arrange/minimize/restore/close all worked correctly,
  close genuinely closed it with no leftover process.
- **`session.js`** now tracks which windows it created vs. which existed
  before it started, and auto-closes its own still-open scratch windows at
  `report_done` (never anything that looks unsaved, never anything the user
  already had open) via one model call asking "which of these hold the
  actual result?" — defaults to keeping everything if that call fails.
- **`open_app.js`** gained a Start-Menu-shortcut search fallback behind the
  existing fixed allowlist — verified against the user's real 107 installed
  shortcuts. One honest limitation found: it only does substring matching,
  so `"vs code"` doesn't resolve to "Visual Studio Code" (no shared
  substring), though the exact name and single-word matches (`"code"`,
  `"obsidian"`) work correctly.
- **A control session can now call a connector tool mid-task** (e.g. update
  a Notion page while also working on the desktop), through the exact same
  confirm gate as a risky desktop action.
- **A new monitoring system** (`server/monitor/`) — "watch for X, then
  (optionally) act." Cheapest-first checks (window/process/file before ever
  falling back to a screenshot + vision model), adaptive polling that backs
  off over time, 2-hour auto-expiry. A distinct amber "Watching for…" bar in
  the UI, separate from and stackable with the existing red control bar.
  Verified live: every check kind, the full `file_size_stable` two-tick
  lifecycle through real timers, the amber bar's restore-on-load and Stop
  button via `agent-browser`.
- One thing deliberately **not** live-tested: an actual AI-driven control
  session moving the real mouse — skipped because the user was visibly at
  the keyboard at that point in the session. Every primitive it would use
  was verified directly instead, and the decision logic was checked by
  hand, line by line.
- **A real, useful finding from the user's own hands-on testing**: both of
  their live tests happened to run while every one of their AI models was
  simultaneously rate-limited. Test 1 (a live control task) genuinely
  couldn't run with zero working models — expected. Test 2 (a scratch
  Calculator not auto-closing) turned out to be the *safety net working
  correctly*: with no model available to ask "should this close," the code
  is deliberately written to default to leaving it open. Also fixed in the
  same pass: the model's own spoken explanation for the failure invented a
  plausible-sounding but false technical excuse ("permissions/security
  block") instead of relaying the real reason — `prompt.js` now explicitly
  tells it to use only the reason a tool actually gave, never invent one.

### Part 3 — Stage 4: isolated code execution + Skills marketplace

- **`server/sandbox/`** — `detect.js` picks `wsl` → `windows-sandbox` →
  `restricted` (in that priority order); `windows-sandbox` is detected
  honestly but was never given a working runner (no scriptable "run this,
  get stdout back" story the way WSL has, and nothing to test it against
  here) — falls through to `restricted` with a clear note rather than
  shipping something unverified. `restricted-backend.js` (a plain stripped-
  env child process, explicitly labelled `isolation: 'weak'` everywhere) was
  fully live-tested: success, a thrown error, a real timeout kill, secrets
  correctly NOT leaking into the child's environment, a path-escape attempt
  safely contained, and — deliberately — confirmed it does NOT actually
  block network or file access it never claimed to block. `wsl-backend.js`
  (the real, strong backend) was written carefully and reasoned through in
  detail but **could not be verified against a real WSL distro** — this dev
  machine has none installed. Needs a real run against an actual distro
  before it's trusted the way the restricted backend already is.
- **`run_code.js`** — a general "run this snippet" skill, always confirmed
  first. A real bug found and fixed in testing: host-folder access reused
  the Files connector's existing allowlist, but didn't normalize the
  path the same way `allow_folder.js` does, so an already-granted folder
  could be wrongly reported as not allowed depending on slash style.
- **Skills can now be installed from three places**: the existing local
  folder/zip path, a new bundled **directory** (`server/skill-catalog.json`
  — moved there from the originally-sketched `data/skill-catalog.json`
  since `data/` is entirely git-ignored and this needs to actually ship),
  and a **pasted link** (`installFromUrl()` — a direct `.zip`, or a GitHub
  repo/subfolder URL). An **Update** button re-fetches from the recorded
  source while preserving on/off state and settings. All of this was
  verified against the real, public `anthropics/skills` GitHub repository —
  not a mock: real subfolder listings fetched over the GitHub API to build
  honest catalog entries, then a genuine install of `skills/algorithmic-art`
  through the actual UI, landing real content on disk. Two real bugs found
  and fixed along the way: an uncaught timeout during a large download's
  body-read crashed the process instead of failing gracefully, and a
  branch-fallback failure reported a generic "not found" instead of the
  real reason (a swallowed timeout, that time — not an actual 404). Also
  found and fixed: an Express route-ordering bug where `/api/skills/
  catalog` was being swallowed by the pre-existing `/api/skills/:name`
  route (treating "catalog" as a skill name) because it was registered
  after it.
- **Skills that ship their own helper scripts can now actually run them**
  — the one deliberate exception to "Jarvis only ever reads a Skill's
  files, never runs them" (see CLAUDE.md for why that rule exists).
  Approval is per-Skill, asked once, remembered after — mirrors
  `allow_folder.js`'s shape rather than the generic per-call confirm gate
  (which would re-ask every single run). Verified live across two separate
  process runs that the approval genuinely persists.
- **A startup guardrail** checks the bundled catalog's entries against
  every real built-in ability/connector-tool name, logging loudly (not
  fatally) on a collision — verified both that it's silent against the
  real catalog and that it actually fires when a fake collision was
  deliberately injected.

### Open items

1. **The WSL sandbox backend needs a real live test** the moment WSL is
   set up on this machine (or any machine this is tested from) — see
   `server/sandbox/wsl-backend.js`'s own header comment for exactly what
   was and wasn't verified.
2. **A live, AI-driven control session** exercising the new close/restore/
   arrange actions and the scratch-window auto-cleanup end-to-end (not just
   each primitive individually) — skipped deliberately mid-session to avoid
   taking over the user's mouse while they were active; safe to run now.
3. Everything else the user asks to try themselves — reasonable next step
   is asking whether they want to test any of Stage 3/4 live, or move on to
   something new.

---

## This session — Connector detail pages, per-tool permissions, guided setup

Plan file (same one, new "Third correction" section):
`C:\Users\HP\.claude\plans\please-explain-things-in-cozy-curry.md`

The user asked for three concrete upgrades to Connectors, reviewed against
Claude's own reference screenshots again: (1) a richer main list — icon,
description, status, search, "Add" moved to the top; (2) a per-connector
**detail page** — description, available tools grouped logically, a per-tool
Always allow/Ask first/Never allow permission, Disconnect; (3) a bigger
built-in catalog (Gmail, Notion, Slack, Google Drive, GitHub) using each
service's real MCP server where one exists. Explicit instruction: confirm
feasibility with real verification before building, only pause if genuinely
unclear.

**A real mid-task correction, twice.** First: started researching live
(WebSearch + `curl` against real OAuth discovery endpoints) but moved toward
building without ever reporting findings back — the user caught this
directly and it was a fair catch; the research itself had already found
something that changed scope (see below), and skipping the checkpoint before
acting on it was the wrong call. Second, later: the approved plan always said
the main Connectors list and the Browse-connectors directory stay two
separate surfaces (main list = only what's added; Browse = the full catalog,
searchable, opened via "Add"). The first implementation pass merged them
into one scattered list and collapsed "Add" into a single custom-connector
button — neither was ever in the plan, a pure implementation slip. Caught by
the user, reverted to the two-surface design the plan already specified,
keeping only what was legitimately new (icons, colored status,
click-to-detail) plus the one real ask: move "+ Add" from the bottom of the
card to the top.

### What the live verification found (before building anything)

Checked directly via `curl` against each real service's OAuth discovery
documents, not assumed from search results or docs pages:

| Service | Real endpoint | Dynamic Client Registration? |
|---|---|---|
| Notion | `https://mcp.notion.com/mcp` | Yes — genuinely one-click (already shipped) |
| GitHub | `https://api.githubcopilot.com/mcp/` | No |
| Slack | `https://mcp.slack.com/mcp` | No — **and** its OAuth docs require an HTTPS redirect URI; Jarvis's is plain HTTP, so whether Slack's authorize step even accepts it is a real, unresolved risk only a live click-through can answer |
| Google Drive | `https://drivemcp.googleapis.com/mcp/v1` | No |

So three of the four newly-requested services need a real "go create your own
app first" guided flow, not a plain Connect button — the user's explicit
choice, given this: **build the guided flow properly**, for all three, each
verified against that service's real, current setup steps (GitHub's OAuth
Apps page, Slack's app-management flow, Google Cloud's OAuth client
creation — all fetched live, not recalled).

### What changed

- **`server/connectors/oauth.js`** — `discover()` fixed for a real RFC
  9728/8414 compliance bug: the original logic assumed a protected resource's
  well-known document always sits at its origin's bare root; GitHub's doesn't
  (its resource is at `/mcp/`, needing the well-known segment inserted before
  the path, not appended after) — found by reading the real 401 response's
  `WWW-Authenticate` header, not guessed. Fixed to try both forms; re-verified
  live against GitHub, Slack, and Notion (no regression) afterward. Also:
  `startConnect()` now relabels a connector's `connectFlow.kind` from
  `oauth_dcr` to `oauth_guided` once manual credentials actually get used —
  bookkeeping only.
- **`server/connectors/catalog.json`** — GitHub, Slack, Google Drive added,
  each `connectFlow.kind: 'oauth_guided'` with a `guide.steps` array written
  from each service's real, live-checked current setup flow. Slack's entry
  also carries a `guide.note` flagging the HTTPS-redirect risk plainly.
- **`server/connectors/index.js`** — a connector's `config.toolPermissions`
  (`{[toolName]: 'always'|'ask'|'never'}`, set from the detail page) now
  overrides the existing automatic risk-classification `confirm` decision
  entirely — `'never'` drops the tool from the model-facing declarations,
  `'ask'`/`'always'` force the confirm gate on/off regardless of what
  `guard.js`'s classifier would have said on its own.
- **`server/connectors/store.js`** — connectors can now carry an optional
  `description` (mainly for a Custom Connector, since official ones get
  theirs from the catalog).
- **`server/server.js`** — the old merged "official connect" route split
  into three: `POST /api/connectors/catalog/:catalogId/ensure` (create the
  record only), `POST /api/connectors/custom` (create-only now, no more
  auto-connect), and one generic `POST /api/connectors/:id/connect` (starts
  OAuth for any existing mcp connector) — this split is what lets the same
  detail page own the whole connect experience for official and custom
  connectors alike.
- **`public/screens/_connector-detail.js`** (new) — the detail page:
  description, guided-setup form (steps + a copy-able redirect-URI box +
  Client ID/Secret fields) when applicable, or a plain Connect button;
  "Available tools" once connected, grouped by a small verb-matching
  heuristic, each with a permission dropdown; Disconnect.
- **`public/screens/_connector-icons.js`** (new) — a small emoji map for
  catalog icon keys (no new asset/dependency), shared by the list and the
  detail page.
- **`public/screens/app-control.js`** — rebuilt (twice, see the correction
  above): the Connectors card lists only added connectors (icon, colored
  status, click → detail page); "+ Add" (now at the top) opens either Browse
  connectors (the full catalog, its own search, in a modal) or Add custom
  connector (now create-only, same as the official flow).
- **`public/style.css`** — new rules for the clickable list rows, the
  detail page's header/tool-group labels/guide steps/redirect-URI box, and an
  explicit green for "Connected" (kept separate from the existing blue
  `.badge.good`, used elsewhere for model availability, so this didn't
  silently re-theme every other "working" badge in the app).

### Two real bugs found and fixed via live testing (not code review)

1. **The RFC 9728/8414 discovery bug above** — GitHub's real server 404'd on
   every naive `.well-known` guess until the fix; Notion and Slack (whose
   resource/issuer happen to sit at their origin root) had been masking this
   the whole time.
2. **A tool-grouping bug**: the grouping heuristic only matched a verb
   anchored to the very start of a tool name (`search_x`) — which silently
   put every one of Notion's real tools (`notion-search`, `notion-fetch`,
   `notion-update-page`, `notion-delete-page` — the service name leads, not
   the verb) into "Other". Found by actually connecting a real service in
   testing and looking at the rendered groups, not by reading the code.
   Fixed by splitting the tool name into words and checking every word.
3. (Caught and fixed during testing, not left in) A search-box race: refetching
   the catalog on every keystroke let an earlier (slower) request's results
   render after a newer one's, producing duplicate/stale rows. Fixed by
   fetching the catalog once and filtering the already-loaded array
   synchronously on each keystroke instead.

### Verified

On a throwaway scratch server (port 3177, fresh data dir — confirmed same
real-instance PID before and after):
- Live `curl` verification of all four services' real OAuth discovery
  documents, both before and after the RFC 9728/8414 fix (GitHub/Slack/Notion
  all re-checked post-fix, no regression).
- The full ensure → connect → (guided form or one-click) path for all four
  catalog entries, including GitHub's guided flow producing a real
  `github.com/login/oauth/authorize` URL with a fake test Client ID.
- The per-tool permission override directly (`'never'` removes a tool from
  `getToolDeclarations()` entirely; `'always'`/`'ask'` force the confirm gate
  off/on against real risky/safe tool names, not just the automatic
  fallback).
- The full UI end-to-end via the `agent-browser` skill (not claude-in-chrome,
  per the user's direction this session): the Add popover, the Browse
  connectors directory with working search, the guided-setup detail page
  (screenshotted — steps, redirect-URI box with the correct live port,
  Client ID/Secret fields all render correctly), and the connected/tools
  view (screenshotted — three synthetic Notion tools landed in the correct
  groups with their permissions persisted and reflected in the dropdowns).

### Open items

1. **The real interactive OAuth login**, for any of GitHub/Slack/Google
   Drive/Notion — needs the user's own click-through; report back what
   happens, especially Slack's authorize step (the flagged HTTPS-redirect
   risk).
2. Stage 4 (Monitoring) is still not started.

---

## This session — Desktop control / Browser / Files settings removed from the interface entirely

Plan file (same one, new "Second correction" section):
`C:\Users\HP\.claude\plans\please-explain-things-in-cozy-curry.md`

The user asked a direct, honest question after seeing a screenshot of the old
App Control page: "I thought Desktop control/Browser/Files were Jarvis's own
capability and shouldn't appear on the interface — did that happen because of
a wrong choice I made?" Answered honestly first: no, it wasn't a mistake —
that four-card layout was in the very first approved plan, before Stage 3 was
even built, and was tested and signed off on at the time. But the underlying
instinct was right, and interviewing (three rounds of `AskUserQuestion`, per
the user's own request to keep asking until I actually understood) surfaced a
real, applicable principle already written into CLAUDE.md for the Skills
screen — "Jarvis's built-in abilities... must never appear in the Skills
screen" — that had simply never been applied to this page.

Confirmed answers before touching any code: (1) the concern is that these
three shouldn't need **any** settings UI at all, not just that they visually
resemble Connectors; (2) the safety blocklist becomes **fixed, non-editable
hardcoded defaults** — no screen, no way to edit it; (3) the Files folder
allowlist instead grows **through conversation** — Jarvis asks the first time
it needs a folder, and remembers the answer; (4) with all three cards gone,
the App Control page **becomes just the Connectors screen**.

### What changed

- **`public/screens/app-control.js`** — `buildDesktopCard`/`buildBrowserCard`/
  `buildFilesCard` deleted entirely; `render()` now shows only the Connectors
  card.
- **`public/nav.js`** — label `'App Control'` → `'Connectors'` (the `id` stays
  `'app-control'` — internal route id, `open_section` skill's enum, nothing
  user-facing reads it).
- **`server/skills/allow_folder.js`** (new) — the only way the Files
  allowlist ever grows now. `confirm: 'always'`, reusing the exact same
  read-back-and-confirm gate `remember_about_me` already uses (not a new
  mechanism) — the model calls it once, gets `needs_confirmation` +
  a plain-language summary, the user says yes, the model calls it again with
  the token, and only then does it actually grant. Refuses a small hardcoded
  denylist (`C:\Windows`, `C:\Program Files`, `C:\Program Files (x86)`) no
  matter what's asked — same "sensible hardcoded floor" idea as the
  blocklist, applied to the filesystem. `meta: true` — can't be triggered by
  an unattended scheduled task, only live conversation.
- **`server/prompt.js`** — one added line: ask the user by name for the
  specific folder needed before ever calling `allow_folder`.
- **`server/connectors/files.js`** — reworded the one error string that
  pointed at the now-removed "App Control screen."
- **`server/server.js`** — removed `GET`/`POST /api/safety` and
  `GET /api/connectors/files`/`GET /api/connectors/browser` (all now-dead —
  confirmed via grep, no other consumer of any of them).
- **A real bug this surfaced and fixed**: the files/browser singleton
  connector records used to only ever get created as a side effect of the
  now-removed cards' GET routes running on page load. A fresh install that
  never visited that page would never have either record, and
  `connectors/index.js` would silently never register `list_files`/
  `browser_navigate`/etc. — losing both abilities invisibly for a new user.
  Fixed by seeding both singletons once, unconditionally, at server startup.

### Verified

On a throwaway scratch server (never the user's real instance — same PID
before and after):
- **The startup-seeding fix, proven correctly**: with a completely fresh,
  empty data directory, and **without ever visiting the app-control page**,
  `connectors/index.js`'s `getToolDeclarations()` already listed
  `list_files`/`read_file`/`write_file`/`move_file`/`browser_navigate`/etc. —
  confirming the abilities are never silently lost for a new install.
- **`allow_folder`'s full flow**, directly: a file read before any grant
  failed with the new plain-language message; the confirm gate correctly
  returned `needs_confirmation` on the first call; the denylisted system path
  (`C:\Windows\System32`) was correctly refused on the confirmed call; a
  legitimate folder was correctly granted on its confirmed call; and a
  `read_file` against that exact folder afterward succeeded.
  (Caught and corrected two of my own test-script mistakes along the way —
  bash quoting eating backslashes, and an unescaped single-backslash JS
  string literal silently dropping its own backslashes — neither was a real
  bug in the shipped code; both were re-verified cleanly once the test itself
  was fixed.)
- **The dead routes actually 404** (`/api/safety`, `/api/connectors/files`,
  `/api/connectors/browser`), while `/api/connectors` still works.
- **The rebuilt page, visually**, via a real browser (`claude-in-chrome`):
  nav reads "Connectors", and only the Connectors card renders — no Desktop
  control/Browser/Files cards.

### Open items

1. Same two as the Connectors rebuild below: only Notion is in the Official
   Connectors directory, and a real human OAuth login click-through still
   needs the user.
2. Stage 4 (Monitoring) is still not started.

---

## This session — App Control's Connectors rebuilt to match Claude's

Plan file (same one as Stage 3, extended with a "Correction" section):
`C:\Users\HP\.claude\plans\please-explain-things-in-cozy-curry.md`

The user reviewed Stage 3's "Connected services" UI against real screenshots
of Claude's own Connectors page and called out a genuine design mistake, not
a cosmetic one: exposing "Command (CLI)" and "HTTP API" as separate
user-facing connector types was wrong. Corrected to exactly two ways to add
an integration — **Official Connectors** (a bundled directory, every entry
just a name + description + one Connect button, no "ready"/"needs setup"
label ever shown) and **Custom Connector** (name + remote MCP Server URL +
collapsible Advanced settings for an optional OAuth Client ID/Secret).
Implementation details stay hidden — the user explicitly required this:
"Internally, Jarvis can still use APIs, SDKs, OAuth, CLI commands, or
anything else it needs, but those implementation details should be hidden
from the user." Real web research (not recalled training data) confirmed
Claude's own connectors are 100% remote MCP over Streamable HTTP with OAuth
2.1 + PKCE, and that "official" entries there are themselves just curated
custom connectors under the hood — the same mechanism serves both paths here
too. Naming: page stays **App Control**, the card is now **Connectors** (both
per the user's explicit answers).

### What changed

- **`server/connectors/oauth.js`** (new) — OAuth 2.1 + PKCE client: discovery
  (RFC 9470/8414), Dynamic Client Registration (RFC 7591) with a manual
  Client ID/Secret fallback, `startConnect`/`handleCallback`/
  `getAccessToken` (silent refresh)/`disconnect`. Redirect lands on Jarvis's
  own already-running server — no separate temporary listener needed.
- **`server/connectors/mcp-remote-client.js`** (new) — the Streamable HTTP
  MCP transport real hosted servers speak: POST JSON-RPC, parses either a
  plain JSON or SSE-framed response, tracks `Mcp-Session-Id`.
- **`server/connectors/index.js`** — `cli`/`http` type handling removed
  entirely; `mcp` now routes between `mcp-client.js` (local stdio — kept as
  hidden internal plumbing) and `mcp-remote-client.js` based on
  `connector.config.connectFlow.kind`.
- **`server/connectors/store.js`** — doc comments updated for the new
  `connectFlow` shape (`{kind: 'stdio'|'oauth_dcr'|'oauth_guided', ...}`);
  `cli`/`http` dropped as connector types.
- **`server/connectors/catalog.json`** — rebuilt. Only **Notion**
  (`mcp.notion.com`) is listed, deliberately — the plan's "catalog honesty"
  rule says an entry only ships once its Connect flow is tested end to end,
  and Notion is the only one actually verified this way so far. (GitHub/
  Slack/Stripe were considered but their exact endpoint URLs were never
  independently verified in this session, so they were left out rather than
  guessed.)
- **`server/server.js`** — new routes: `GET /api/connectors/catalog` (now
  returns `connectorId`/`status` per entry instead of a boolean `installed`),
  `POST /api/connectors/official/:catalogId/connect`, `POST
  /api/connectors/custom`, `GET /api/connectors/oauth/callback` (returns a
  small HTML page, broadcasts an SSE `connector_status` event). `test`/
  `DELETE` routes updated for the new `connectFlow`-based dispatch; the old
  http-secret-patch code path in `PATCH /api/connectors/:id` was removed
  (no `http` type left to need it).
- **`public/screens/app-control.js`** — the "Connected services" card
  rebuilt and renamed to "Connectors": a directory modal (search-free list,
  one Connect button each, live-polls until a connection completes) and a
  Custom Connector dialog (`<details>` for Advanced settings, same in-modal
  polling pattern). Both open the provider's real sign-in page in a new tab
  via `window.open`.
- **`public/app.js`** — added a `connector_status` SSE handler so a plain
  system note appears regardless of which screen is open.

### Verified

Real, live testing against a throwaway server (port 3177, scratch data dir —
confirmed the real port-3000 instance's PID was unchanged before and after):
- **Discovery + Dynamic Client Registration against the real, live Notion
  server** — got back a genuine `client_id`, correct PKCE challenge, correct
  redirect URI.
- **CSRF protection**: a callback hit with a tampered `state` was correctly
  rejected ("invalid or already used").
- **Official and Custom connectors use the identical mechanism**: adding a
  Custom Connector pointed at `https://mcp.notion.com/mcp` produced the same
  shape of authorize URL as the Official entry.
- Bad-URL input on the Custom Connector route correctly rejected with a
  plain-language message (`"That doesn't look like a valid URL."` — fixed
  during testing; it originally leaked a raw `Invalid URL` from `new URL()`).
- **Full OAuth mechanics against a local stub** (combined fake authorization
  server + Streamable-HTTP MCP server, since a real end-to-end login needs a
  human): code → token exchange → secret storage → status flips to
  `working`; `tools/list` returned as an **SSE-framed** response and parsed
  correctly; a real `tools/call` through the full merged tool-declaration
  path (`stub_service__echo`) round-tripped correctly; and — the one the plan
  specifically asked to prove — **letting the token expire triggered a
  silent refresh** (`getAccessToken()` returned a token literally named
  `stub-access-refreshed-1`), never a second Connect prompt.
- Not-yet-connected `test` calls fail gracefully ("This connector is not
  connected yet."), and `DELETE` correctly disconnects/removes.

**Not yet verified, and can't be by me**: an actual human login/consent
click-through against a real provider. The mechanics up to and including
token exchange are proven against the stub; only the "click Allow in a real
browser tab" step itself needs the user. Try Notion first — `Connect` on it
from Official Connectors, approve in the tab that opens, confirm the row
flips to "Connected" and a live tool call succeeds.

### Open items

1. **The real interactive OAuth login** — needs the user's own click-through
   (see above). Report back if the tab doesn't redirect correctly or the row
   never flips to "Connected."
2. **Only Notion is in the Official Connectors directory.** GitHub/Slack/
   Stripe/Google/others need their exact endpoints verified for real (per the
   plan's "catalog honesty" rule) before being added — don't add one on the
   strength of "the transport should work in theory."
3. Stage 4 (Monitoring) is still not started.

---

## This session — Content Analysis + Planning Partner rebuilt from a refined spec

**Not yet tested against a real model.** Everything below was verified with
a scripted stub model standing in for a real one (see "Verified" below) —
that proves the plumbing is wired correctly, not that a real model will
actually phrase things well or reach for the right tool at the right
moment. **This is the first thing to try** when picking this back up — see
"How to test this" at the end of this section.

The user supplied a refined, plain-language specification of what both
features were always meant to be and asked for an audit against it before
any code changed. The audit's finding, in short: neither failure was a bug
sitting on good foundations — the *shape* of both engines was the problem.

- **Content Analysis** was a fact-checker with a general-purpose bucket
  attached, not the other way round. A hardcoded template ran on *every*
  piece of shared content — a photo, a contract, a design article — hunting
  for "earnings, timescales, guarantees, before-and-after results," and it
  produced a full summary before ever asking what the user wanted, which is
  exactly the automatic-analysis behaviour the spec forbade.
- **Planning** was a conveyor belt: mentioning an idea immediately
  auto-researched it and generated a fixed queue of up to six questions.
  Every reply after that got filed as "the answer to question N" — including
  a challenge to the idea itself ("why would I need a whole checkout system
  for that?") — and the plan wrote itself the instant the queue emptied,
  with no go-ahead from the user at any point.

Per the user's explicit instruction, **both were deleted outright and
rebuilt from the spec** — including the parts that already worked
correctly (research-before-verdict, per-assistant prompt tailoring), on the
reasoning that the shape producing the defects had to go even where one
particular behaviour growing out of it was fine. See this session's full
conversation for the complete audit; this section is the condensed record.

### What changed

**Deleted outright**: `server/analysis/`, `server/planning/`, the old
`server/research.js`, and five skills (`analyze_content`,
`ask_about_content`, `fact_check`, `plan_project`, `answer_plan_questions`).

**Built new**: `server/content/` (`content-store.js`, `intake.js`,
`investigator.js`) and `server/projects/` (`project-store.js`,
`assistants.js`, `project-engine.js`), plus `server/research.js` rebuilt at
the same path with the same design (free web search first, model-native
search only if that comes back thin — `toSearchQuery()`'s keyword reduction
and the DuckDuckGo parsing carried forward verbatim, since both were
measured the hard way). Nine new skills: `share_content`,
`examine_content`, `check_claim`, `look_it_up`, `start_project`,
`note_project_decision`, `research_project`, `write_project_plan`,
`write_build_prompts`.

**The one deletion that actually fixes the rigid-mode complaint**: the
`questions[]`/`answers{}` queue is gone from the project record entirely —
not patched, removed. There is nothing left for a challenge or a change of
mind to be mis-filed into. `decisions[]` replaces it: one entry per thing
actually settled, added as the conversation produces one, not gathered as a
queue to drain.

**The one behavioural rule enforced everywhere content enters the
conversation**: sharing something (a link, a file, an attachment) costs
zero model calls and produces no analysis until the user says what they
want. `content/intake.js`'s `identify()` is the free glance — title, rough
kind, length/size, from cheap metadata alone. `server/attachments.js`'s
video/audio branch was rewired to match: it used to auto-ingest the instant
a file arrived; it now only registers it, same as a shared link.

`media.js` was split: the parts specific to Content Analysis
(`classifySource`, `describeSource`, YouTube metadata/caption fetching,
`prepareAttachment`) moved into `content/intake.js`; the genuinely shared
parts (`fileKind`, `mimeTypeFor`, `inlineAttachment`, `fetchArticle`,
`stripHtml` — used by the composer's paperclip, `read_web_page`, and the
new `research.js`) stayed in `media.js`.

`prompt.js`'s shared-content/planning instruction block was rewritten to
match: ask before analysing; after `start_project`, just talk — respond to
a challenge or tangent in place, never advance a stage without a clear
go-ahead. `app.js` gained multi-prompt document-card rendering
(`addPromptCards()` — one card per prompt, plus a system note for the
recommended order when there's more than one); its `TOOL_LABELS` map (the
"Checking the time…"-style status line shown while a tool runs) was
initially left with the five deleted skills' old labels and none of the
nine new ones — caught and fixed in the same pass as this write-up, not
during the original build.

**Explicitly out of scope, on the user's instruction**: conversation
persistence/memory. The chat itself still resets on restart and still keeps
only the last ~60 entries — the user has a separate plan for real
memory/persistence and didn't want a partial fix here that would just have
to be undone later. The one mitigation built in: `decisions[]` is real,
on-disk, per-project state, so a project's substance survives a restart
even though the casual back-and-forth around it doesn't.

### Verified

All of the following ran against a real, isolated scratch server
(`PORT=3177`, scratch `JARVIS_DATA_DIR`, no real `.env` needed) with a
~150-line `node:http` stub standing in for the model (OpenAI-compatible SSE
shape, scripted per-request via a `/_script` queue and a `/_calls` counter
endpoint added just for this testing) — the user's real port-3000 instance
was confirmed running throughout and never touched:

- **Zero model calls before asking**: shared a real YouTube URL with no
  instruction; the stub's call counter stayed at 0 while `identify()` still
  correctly pulled the real title via oEmbed. This is the single behaviour
  the whole Content Analysis rebuild exists for.
- **The rigid-mode fix, through the real tool-calling loop**: `start_project`
  on a real idea, then a scripted reply that *challenges* the idea
  ("why would I need a checkout system just to show my paintings?"). Assert:
  the reply engages with the challenge directly; nothing auto-advanced
  (no research/plan/prompts ran); nothing got filed as a decision.
- **Decisions reaching both the plan and the prompt**: two decisions noted
  mid-discussion were confirmed present in the actual PROMPT TEXT sent to
  the model for both `writePlan` and `writePrompts` — not just in the
  finished plan document, which is what the old build's specific bug was.
- **Single vs. multi-prompt**: a small idea produced one prompt with
  `promptOrder: null`; a large idea produced three prompts with a real
  recommended-order explanation.
- **The cache/`needsAnotherLook` mechanism**, against a real (tiny) test
  image: first question = 1 model call, observations cached. A follow-up
  the cache covers = 1 call (answered from the cache). A follow-up the
  cache doesn't cover = 2 calls (a cache check that says no, then a genuine
  second look) — and the final answer demonstrably came from the second
  look, not the stale cached note.
- **`judgeClaim()`'s research-before-verdict guarantee**, live: a real
  DuckDuckGo/Wikipedia search returned 4 real sources about a real claim
  (the Great Wall of China visibility myth) before the stubbed verdict step
  ever ran.
- **One live document card**, screenshotted: a `check_claim` turn run
  through the real chat pipeline, with the browser's real SSE connection
  live, produced a correctly rendered card — title, verdict badge, full
  write-up, real clickable sources.
- `node --check` on every changed/new file.

### How to test this

1. **Share something with no instruction** (a link, a file, pasted text)
   and confirm Jarvis says what it appears to be and asks, rather than
   summarizing, researching, or checking anything on its own.
2. **Mention a project idea**, let Jarvis ask something, then challenge it
   or change your mind mid-conversation. Confirm it responds to that
   directly instead of treating your reply as an answer to file away and
   moving on to the next stage.
3. **Ask it to research / write the plan / write the build prompt**
   separately, each as its own explicit ask, and confirm none of the three
   happens on its own — research finishing shouldn't trigger the plan; the
   plan finishing shouldn't trigger the prompt.
4. For a larger idea, confirm `write_build_prompts` can produce more than
   one prompt with an explained order; for a small one, confirm it produces
   just one.

## Earlier session — Planning + Content Analysis folded into the conversation

*(**Superseded by the session above.** Both engines described here —
`server/planning/` and `server/analysis/` — were deleted outright and
rebuilt from a refined spec as `server/projects/` and `server/content/`. The
join this section describes (one shared conversation transcript,
`sessionId` on every record) is still exactly the design; the specific
files, the digest-first ingest, and the `questions[]`/`answers{}` queue it
describes are gone.)*

Plan file: `C:\Users\HP\.claude\plans\hidden-dancing-wombat.md`

The user's own diagnosis, and it was right: separate pages broke the
connection between the two. Sharing a video mid-plan couldn't inform that
plan. **The pages were the symptom, not the cause** — there were three
unconnected memories (`conversation.js`, `plan-store.js`,
`analysis-store.js`), and both engines drove models through `ai.js`'s
`askModel()`, which is stateless by design. The planner had never seen the
conversation; the chat model had never seen a digest.

### What changed

**Both drawer sections are gone**, along with `public/screens/planning.js`,
`public/screens/analysis.js`, and every `/api/plans*` and `/api/analyses*`
route. The engines and stores under `server/planning/` and `server/analysis/`
are unchanged and still do all the work — what went away is the doors.
Plans and verdicts now travel **on** the SSE event and render as document
cards in the transcript (`app.js`'s `addDocumentCard`, reusing
`screens/_markdown.js`).

**The join that makes it work**: `analyzer.js`'s `pushDigestToConversation()`
puts each digest into the transcript when reading finishes; `planner.js`'s
`conversationContext()` reads that same transcript into its question and plan
prompts. Records now carry a `sessionId`.

**The five hardcoded intent tracks are deleted.** `classifyInstruction` /
`INTENT_PATTERNS` used to route the user's wording into factcheck / realism /
save / summarize / other, which capped what could ever be asked. Replaced by
`judgeClaim()` plus a new **`fact_check` skill** the model composes freely.
The research-before-verdict guarantee moved *inside* `judgeClaim()` — it is
now impossible to skip rather than merely likely to be routed correctly.

**Attachments from the main composer** (paperclip, drag-anywhere, paste):
new `server/attachments.js` + `POST /api/uploads`. Two intake modes — inline
(images and text documents ride in the message; images stay at full fidelity,
the deliberate Claude-like choice) and digested (video/audio go through
ingest, only the digest joins the conversation). See CLAUDE.md's new
"Attachments" section.

**The reported bug — choosing a picture failed instantly.** Three causes,
all fixed and all written up in CLAUDE.md's gotchas: images never needed a
provider upload API at all; every OpenRouter model falsely claimed vision;
and `pickModel` had no fallback. Vision-capable models went 39 → 23, with
Gemini now leading instead of a *music* model.

### Verified against the real API

- **An image genuinely reaching a model**, twice: directly (a generated
  3-row red/green/blue PNG, described correctly) and through the full
  `/api/chat/stream` path with **no message text at all** — it survived
  **ten** consecutive model failovers and the eleventh described it right,
  proving `media` replays intact on the neutral transcript.
- `pickModels({need:{vision:true}})` against the user's real 39-model list,
  before and after the capability fix.
- Text documents readable with no model capability at all; `../../.env`
  correctly refused by `getUpload()`'s id validation.
- One shared transcript across analyzer + planner + runner.
- No dangling references to the deleted screens anywhere; all front-end
  assets still serve; `fact_check` registered in the live tool list.

### Open items

1. **The end-to-end connection has NOT been run live**: "start a plan → share
   a video mid-plan → confirm the plan references it without retyping." The
   plumbing is verified piece by piece, but the user's Gemini quota ran out
   mid-testing (20/day). **This is the single most valuable thing to try
   first** — it is the whole point of the change.
2. **The other session left `server.js` mid-refactor.** Six identifiers are
   referenced but not imported — `listInstalledSkills`, `installPlaybookSkill`,
   `updatePlaybookSkill`, `removePlaybookSkill`, `listCatalog`,
   `getCatalogEntry`. `node --check` passes (valid syntax, undefined at
   runtime), so the server boots but `/api/skills/installed` and the
   install/edit/delete routes throw when hit. Deliberately **not** fixed here
   — guessing the intended module risked clobbering work in progress. It
   appears to be a move from `skill-store.js` to a folder-based
   `skills-fs.js`.
3. Video still has never round-tripped against the live API (see the
   superseded section's open item 2 — images now have, video has not).

---

## Earlier session — Planning + Content Analysis, built as pages

*(**Superseded by the session above.** The engines, `ai.js`, `research.js`,
`media.js` and the capability design below are all still current and correct.
What no longer exists: the two drawer sections, both screen files, the
`/api/plans*` and `/api/analyses*` routes, `#/planning` and `#/analysis`
routing, and `classifyInstruction`'s five intent tracks.)*

Plan file: `C:\Users\HP\.claude\plans\i-m-continuing-to-upgrade-cryptic-pillow.md`

Two new drawer sections under a **Thinking** group: **Planning** (rough idea →
researched MVP plan → a prompt written for whichever AI will build it) and
**Content Analysis** (hand it a video/article/file → it reads it once → ask it
anything about it, with independent research behind any verdict).

### What was built

**Foundations (shared).** `server/ai.js` — the third way to drive a model
alongside `runner.js` and `control/session.js`: one prompt, one answer, no
tools, tolerant JSON parsing, capability requirements enforced at the call
site. `server/research.js` — Jarvis genuinely looking things up, which it
could not do before at all. `server/media.js` — working out what the user
handed over and turning it into text or an attachment.

**Capabilities are not hardcoded to Gemini.** This was an explicit user
requirement, raised by them when an earlier draft implied "if video, call
Gemini". Adapters export a `CAPABILITIES` ceiling, models carry their own
`caps`, and `ai.js` filters on both. Gemini is the only adapter implementing
video/search *today*; adding another is one function plus a flag, with no
change to Planning or Content Analysis.

**A real bug fixed on the way through:** `tts.js`, `turn-check.js` and
`live.js` all read `GEMINI_API_KEY`, which is not in this user's `.env` — their
Gemini key lives as a connection secret (`JARVIS_SECRET_CONN_GEMINI`). Voice
output and turn detection had been silently dead. New `server/gemini-key.js`
checks both; all three now use it.

### Verified

Full pipeline end-to-end on an isolated server (port 3177, scratch data dir,
never the user's port 3000 or their other session's 3099):

- Plan: research → questions → answers (with one deliberately skipped) → plan
  document → handoff prompt → retarget to a custom-named assistant. All four
  steps recorded with which model ran them.
- Analysis: article and uploaded-document ingest; `awaiting_instruction` when
  no instruction was given (the required "ask, don't assume" behaviour);
  realism check producing a verdict, what's-left-out, the step-by-step
  breakdown, and 4 real sources; save-notes writing to the record.
- Upload endpoint including a path-traversal attempt (`../../.env` → written
  safely inside `data/uploads/` as `mselo44n-env`).
- ~~Both screens in a real browser: markdown rendering, copy-prompt box, verdict
  badges, source links, drawer entries, `#/planning` and `#/analysis` routing.~~
  **(No longer applicable — both screens and both routes were removed. The
  markdown rendering, copy-prompt box, verdict badges and source links all
  survive; they render in the transcript now.)**
- The circular-import invariant, proven by walking the import graph from all
  four new skills.

**Not verified against a real model.** Every model the user has was rate-
limited throughout, which is why a `node:http` stub stood in (see CLAUDE.md's
gotcha on this — it's the right tool, and worth rebuilding when quota blocks
testing again). The failure path *was* verified against the real thing, and
reports quota exhaustion in plain language rather than a raw 429.

### Open items

1. **Run it once against a real model** when quota allows — the stub proves
   the plumbing, not the prose quality of a plan or a verdict.
2. **Video has never been exercised against the live API.** No video-capable
   model was reachable. `prepareAttachment` → `uploadFile` (Files API upload +
   ACTIVE polling) and the YouTube-URL-as-`fileData` path are both written to
   the installed SDK's real type definitions but have not round-tripped for
   real. Test this first when a Gemini model is available.
   **(Still open. Images HAVE since round-tripped live — see the current
   session above — but they take the inline base64 path, not `uploadFile`,
   so this says nothing about video.)**
3. **YouTube captions can no longer be scraped** (verified — see CLAUDE.md).
   The fallback without a video model is title + description. If captions
   matter, the honest options are a video-capable model or a proper
   transcript API, not more scraping.
4. `public/screens/tasks.js` still has its own near-identical copy of the
   model picker; the shared one now lives in `_ui.js` as `modelPicker()`.
   Left alone deliberately to avoid colliding with the concurrent session —
   worth collapsing next time tasks.js is edited.

---

## Earlier session — App Control / Skills / Computer Control / App Control connectors (Stages 1–3 of 4)

Full plan (all 4 stages, decisions, file lists):
`C:\Users\HP\.claude\plans\please-explain-things-in-cozy-curry.md`

Building toward: Jarvis controlling apps/services generally (not just its
existing fixed skills), a Skills library the user can browse/install/write,
and — later stages, not started — App Control connectors (browser/files/
email) and Monitoring (ambient awareness + standing instructions). Four
stages, each meant to be tried by the user before the next is built.

### Stage 1 — Skills system + safety spine (done, user has not yet objected)

Playbook Skills (`server/skill-store.js`, `server/skill-catalog/`) — install
from a bundled catalog or write your own in the Skills screen
(`public/screens/skills.js`), pure instructions, never downloaded code.
Merged into the normal tool list `skills/index.js` already builds. The safety
spine: the always-on-top red overlay (`control/overlay.ps1` +
`overlay-bridge.js`), the in-page control banner, the Stop endpoint, and
`control/safety.js`'s blocklist (banking/password-managers/Windows UAC by
default). Verified live: installed/edited/removed a Skill through the actual
UI, showed/hid the overlay for real, hit Stop for real.

### Stage 2 — Computer control (built, infrastructure-verified, one gap still open)

The look/act/verify loop: `control/agent.ps1` (PowerShell primitives) +
`control/ps-bridge.js` (the persistent process) + `control/session.js` (the
loop) + `skills/control_computer.js` (the chat entry point, confirm-gated on
a generated plan). New capability flags threaded through
`conversation.js`/all 3 adapters/`catalog.js`/`router.js` so the loop can
show a screenshot only to a model that can actually see one.

**What's proven with real, hand-run tests today** (see CLAUDE.md's new
"Computer control" section and its gotchas for the mechanics):
- `agent.ps1`'s every command, run by hand against a real Notepad window —
  including `read_window` pulling exact real text/values, `type`/`key`
  correctly landing (after the focus-first gotcha was found and fixed), and
  the newly-added `minimize_window` genuinely minimizing (confirmed via
  off-screen bounds) and un-minimizing via `focus`.
- A live control session (real free OpenRouter model) launching a brand-new
  Notepad window from nothing (`launch_app`) — proven twice.
- The mouse-movement safety stop firing correctly on real mouse activity —
  three times, live.
- The blocklist firing correctly the moment a blocked window came to
  front — twice, live (see "Open items" for what got corrected here).
- Stop, the plan-approval confirm gate, and the SSE-driven control banner —
  all exercised end-to-end.

**What is NOT yet proven**: a single unbroken live run reaching
`report_done` (open → type → save → verify → done). Every attempt today got
legitimately interrupted by one of the safety checks above firing on real
desktop activity (this is a real desktop in daily use, not an isolated VM),
and then the OpenRouter account's free daily quota ran out. The user will
test this themselves once quota resets and report back if anything's wrong
— **do not assume this path works from code review alone; it has not been
watched to completion.**

### A real, serious mistake made this session — read before touching Notepad/multi-window apps

While cleaning up a disposable test Notepad window, `Stop-Process` on it
**also closed the user's real, unrelated Notepad window with real unsaved
content** — Windows 11's built-in Notepad can share one process across
several open windows, confirmed by hand afterward. No data was lost (Windows
11 Notepad autosaves and restored the tab on next launch), but this was
found, not prevented. **Never `Stop-Process` a shared-host app to clean up a
test window** — use the app's own UI instead (see CLAUDE.md's gotcha).

### A design correction made mid-session, from direct user feedback

The user caught two real problems by testing this live:
1. **VS Code got permanently blocklisted by mistake.** The user's actual ask
   was "minimize VS Code instead of closing it during a test," which was
   misread as "Jarvis should never be allowed to touch VS Code at all." Fully
   reverted — see `control/safety.js`, no default block on VS Code exists.
2. **The bigger, structural catch**: the control loop could only ever act on
   the front window or launch something brand-new — it had no way to bring
   an *already-open* window forward, meaning any multi-window task would have
   had no path except (in principle) closing something to get to something
   else. Fixed: `switch_window`/`minimize_window` actions, `perceive()` now
   lists every open window, `guard.js` checks the actual target window for
   these two actions, and `agent.ps1` still has **no close/kill command at
   all** — closing stays a deliberate future feature behind the confirm gate,
   never a casual model action.

Both fixes are in the plan file's Stage 2 "Correction" section and in
CLAUDE.md's gotchas — read those before extending the control loop further.

### Stage 3 — App Control connectors (done and fully live-tested — all 5 types)

`server/connectors/{store,files,browser,mcp-client,index}.js` + `get-catalog.js` +
`catalog.json` + `public/screens/app-control.js`. Every enabled connector's tools merge
into `skills/index.js` as a third source (built-ins, playbooks, now connectors) — see
CLAUDE.md's new "App Control connectors" section for the architecture.

**Unlike Stage 2, this one has a real end-to-end proof for every connector type**,
each run for real against a throwaway server (never the user's real instance):
- **Files**: write → read → list → move, all through the actual `runSkill()` path, plus
  a path-traversal escape attempt (`../../../.env`) correctly refused.
- **Browser**: the CDP connector launched a real Chrome, navigated to a real page,
  read its actual text back, and typed into a real search box on a live site
  (DuckDuckGo) — this is Stage 3's explicit "done when" proof point, and it passed.
- **CLI**: the bundled catalog's one entry (check disk space) installed, tested, and
  run for real, returning real `Get-PSDrive` output.
- **MCP**: a minimal stdio stub server written specifically to test this (no real MCP
  server was available) — full handshake, `tools/list`, and a `tools/call` round-trip
  all worked, both in isolation and through the full merged-into-skills path.
- **HTTP**: a real GET to a public endpoint with `{placeholder}` substitution in the
  URL, confirmed via the echoed response that the right value actually landed in the
  actual request.

**A real, non-obvious bug was found and fixed via this testing**: `connectors/
browser.js`'s Chrome launch failed intermittently right after a cold start — Chrome's
CDP HTTP endpoint can start responding before its internal engine is fully warmed up,
so a command sent on a freshly-opened WebSocket within about the first second of
Chrome's own process life could go completely unanswered. Fixed with a flat ~1.2s pause
before ever opening the WebSocket. Full diagnosis story (worth repeating the technique
for any future CDP-timing bug) is in CLAUDE.md's gotchas.

**Deliberately modest**: the bundled connector catalog only has one entry (the CLI disk-
space example) — no MCP catalog entries (Gmail, Notion, ...) were added, since there's
no way to responsibly verify a real MCP server target without one on hand. "Connected
services" still supports adding an MCP/CLI/HTTP connector manually regardless.

### Also discovered this session: another Claude Code session is active on this same project

The user confirmed a second, separate Claude Code session is building
"Planning" and "Content Analysis" features in this same repo concurrently
(`server/ai.js`, `server/gemini-key.js`, a `CAPABILITIES` export added to all
three adapters, `nav.js`/`prompt.js`/`server.js` all touched by it too).
So far every change from that session has been additive/compatible with this
build's own edits to the same shared files — confirmed by reading `adapters/
index.js` and `catalog.js` directly rather than assuming.

**Update (current session): `git init` HAS since happened — but there are
still ZERO commits.** `.git/` exists, the branch is `main`, and every single
file is untracked. So the warning's substance stands exactly as written: there
is no safety net if either session rewrites a shared file wholesale. What has
changed is the advice — don't suggest `git init`, suggest a **first commit**,
which is now a one-step ask rather than a setup job. Verify with
`git rev-list --count HEAD` (it errors, rather than printing 0, when nothing
has ever been committed).

### Open items

1. **Live-verify the full Stage 2 happy path** once OpenRouter quota resets
   (or a paid/different key is available) — the user said they'll do this
   themselves and report back. Don't re-run a live control-loop test
   speculatively; wait for them to raise an issue, or ask before spending
   more of their quota.
2. Stage 4 (Monitoring) is not started — the only remaining stage.
3. Stage 3's MCP catalog is deliberately empty beyond the one CLI example —
   a real catalog entry (Gmail, Notion, ...) needs an actual server to point
   at; add one the first time there's a real target to test against, not
   speculatively.
4. Scratch test artifacts from this session were cleaned up (scratch data
   dirs, temp servers, the test Chrome instance and its dedicated profile
   dir under `data/browser-profile`, the stub MCP server process) but a few
   disposable empty test Notepad windows were deliberately left open on the
   real desktop rather than risk another `Stop-Process` mistake — harmless,
   no real content, safe to close manually whenever convenient.

---

## The session before that — model discovery, availability tracking, task popup

Plan file: `C:\Users\HP\.claude\plans\c-users-hp-pictures-screenshots-screens-cached-sifakis.md`
(full design history for both rounds below, including the reference-screenshot
attribution table and every clarifying question asked).

### Round 1 — model discovery, availability tracking, task popup rebuild

**Every provider can now discover its own models**, not just OpenAI-compatible
servers. `server/adapters/{gemini,anthropic,openai-compatible}.js`'s
`listModels(entry)` all now return real `{model, label, contextTokens,
billing}` objects (previously Gemini/Anthropic stubbed `return []`, despite
their installed SDKs genuinely supporting `ai.models.list()` /
`client.models.list()` — verified directly against the installed `.d.ts`
files, not docs). `billing` is `'free'|'paid'|'local'|null` — OpenRouter-shaped
hosts expose a real `pricing` object read straight off the raw response
(undeclared in the SDK's typed `Model` interface, but present at runtime);
Gemini/Anthropic have no pricing field, so `billing: null` there, backfilled
server-side by `catalog.js`'s new `inferBilling()`.

**Real, measured model availability**, not just "has a key." New
`server/models/error-kind.js` classifies a failure into
`quota|no_access|auth|network|other`. `server/models/health.js`'s single
5-minute cooldown became tiered per kind (network 1min, quota 30min,
no_access/auth 6h, other 5min) so a model with no access stops being retried
every turn, but always comes back on its own once the cooldown expires —
including the day billing access changes. Every model record gained
`billing` and `availability: {state, checkedAt, detail}`, written by
`runner.js` after every real turn and by `server.js`'s
`/api/models/:id/test` + new `/api/models/recheck` route after a manual test.

**The "Create a task" popup was rebuilt** — wider (`size:'wide'` on
`openModal`, `.modal-dialog.modal-wide` → 700px), matching an approved
Grok-structure/Claude-width reference blend (full attribution table in the
plan file). New shared primitives in `public/screens/_ui.js`: `iconTile`,
`segmented`, `counterButton`, `popover`, `noticeBox`.

**Per-task model selection existed before this session** — this round moved
it into the new toolbar and *proved* the automatic-fallback mechanism
actually works end-to-end (pinned a task to a deliberately-broken model,
watched it fail over to a working one, confirmed `switchedFromLabel`/
`switchReason` in the persisted run record), since an earlier session had
only described it without confirming it live.

### Round 2 — corrections after the user tried it for real

1. **Free/Paid filter added** next to Select all/Select none in the "Add a
   model" checklist (`public/screens/models.js`'s `buildModelPicker`) — two
   checkboxes, combined with the existing text search. Select all/Select
   none now only touch currently-visible rows (previously touched every row
   regardless of filter, which would have silently defeated the point of
   filtering before bulk-adding).
2. **"Connectors" is now an inert "Connectors — coming later" badge**, not a
   picker over `open_app`/`open_website`/`web_search` — those are Jarvis's
   own built-in abilities, not external integrations, and don't need
   per-item toggles. Styled/worded to match Calendar/Email's "not yet
   connected" pattern on the Morning Briefing page.
3. **"Skills" was removed entirely from the task popup** — no button, no
   popover. Wasn't part of the agreed scope; the user has a *different*,
   later feature in mind (a prebuilt-skills picker) and didn't want this one
   standing in for it.
4. **The per-connector permission notice became one fixed, blanket message**
   (shown for prompt-mode tasks only, gated by one "I understand" checkbox) —
   confirmed with the user via a clarifying question rather than assumed,
   since silently dropping the unattended-use heads-up would have been a
   real safety regression relative to their original spec.

`action` for a prompt-mode task built by this popup is now just
`{type:'prompt', text, modelId}` — no `connectors` key at all. Absent means
"unrestricted" to `runner.js`'s existing contract, which is correct now that
there's no picker left to build a narrower list from. The server-side
allowlist plumbing itself (`skills/index.js`'s `includeMeta` filter,
`runner.js`'s `opts.allowedTools`, `scheduler.js` reading
`task.action.connectors`) was deliberately left in place, dormant — it's
exactly what a future real Connectors picker or the user's planned
"prebuilt skills" feature would reuse.

**Also answered, no code change:** the "Morning briefing" option inside task
creation is not a duplicate of the Morning Briefing page — the page owns the
one global content config (`data/briefing.json`); a briefing-mode task is
purely a trigger that calls the same `composeBriefing()` the page's own
"Preview" button calls. Full trace in the plan file's Context §4.

### Files touched this session

**Server:** `adapters/{gemini,anthropic,openai-compatible}.js`,
`models/{catalog,registry,health,runner}.js`, `models/error-kind.js` (new),
`skills/index.js`, `scheduler/{scheduler,task-store}.js`, `server.js`.

**Browser:** `screens/{models,tasks,_modal}.js`, `screens/_ui.js` (new),
`style.css`, `app.js`. Round 2 touched only `models.js` and `tasks.js`.

### A real bug found and fixed during Round 1 verification

`.instructions-box` (the Instructions textarea + toolbar wrapper) has
`overflow: hidden` and sits inside `.modal-body`, a column flex container.
Per the flexbox spec, a flex item's automatic `min-height` resolves to `0`
(not its content size) whenever its `overflow` isn't `visible` — so it was
the *only* item in that flex column allowed to shrink when the popup's
content exceeded modal height, and got crushed to ~2px while every sibling
field refused to shrink. Fixed with `flex-shrink: 0` on `.instructions-box`
(`style.css`, with a comment explaining why — worth reading if any future
`overflow:hidden` element inside a flex column starts behaving strangely).
Caught only by measuring real `getBoundingClientRect()` values in a live
browser — invisible from reading the code or CSS alone.

Also fixed in the same pass: the model picker's "working" checkmark was
checking `m.ready` (has a key configured) instead of
`m.availability?.state === 'working'` (confirmed working right now) — every
model showed a checkmark regardless of real status.

### How verification was done (repeat this pattern if picking work back up)

Never touch the user's real `data/`/`.env`/port 3000 — check
`netstat -ano | grep ":3000"` first, always. Throwaway instance:
```
PORT=3099 JARVIS_DATA_DIR=<scratch>/test-data JARVIS_ENV_PATH=<scratch>/test.env node server/server.js
```
A small `node:http` stub (`<scratch>/fake-openrouter.mjs`) on port 18081
served an OpenRouter-shaped `/v1/models` with a real `pricing` object, so
billing parsing was exercised against real JSON, not a mock. `agent-browser`
(the skill, not raw Playwright/Puppeteer) drove the actual popup UI —
`agent-browser skills get core` for the command reference; note
`agent-browser scrollintoview @ref` is needed before clicking anything below
the fold inside a scrollable `.modal-body`. Always kill only the 3099/18081
processes by PID at the end, never a blind `taskkill` sweep — both were
stopped cleanly and the real port-3000 instance was confirmed untouched
(same PID, still listening) after every verification pass.

The fallback-mechanism proof: added a model backed by a genuinely dead port
directly via `registry.js`'s `addConnection`/`addModel` (bypassing the
connection-test gate `POST /api/connections` enforces), pinned a task to it,
ran it, and confirmed the run record showed `switchedFromLabel`/
`switchReason` and `ok:true` on the fallback model — not just reading the
code, but watching the switch happen and checking the persisted JSON.

### Open items

None outstanding from the user as of the end of this session — all four
Round 2 requests were addressed (3 code changes + 1 direct explanation) and
verified against a live browser + the persisted task JSON. If resuming, a
good first move is asking the user whether the Round 2 changes look right in
their own real instance before doing anything else.

---

## Earlier session — connections, task/briefing popups, per-task model pinning

*(This is the foundation the most-recent session built on top of. Kept for
history; some specifics below — especially the task-popup consent-notice
description — have since been superseded by the "Most recent session"
section above, e.g. Skills-based task actions and their per-skill consent
text no longer exist in the UI.)*

Built: multi-model support (any provider, not locked to one), task
scheduling, a morning briefing, voice-mishearing confirmation, drawer
navigation, grouped model "connections" with combined discover+add,
popup-based creation for models/tasks, per-task model choice with the same
fallback chain as the rest of the app, and a general "any Skill can be a
briefing source" model replacing old fixed weather/headlines checkboxes.

Its own plan file (superseded path, kept for reference):
`C:\Users\HP\.claude\plans\i-m-upgrading-my-existing-snazzy-pnueli.md`

### Standing operational guidance (still applies)

**Check whether a server is already running** before touching it — see
`CLAUDE.md`'s "Run it" section. Don't kill/restart the user's real instance
without checking with them first if they might be using it.

**`.env` note from that session:** `GEMINI_API_KEY` was present but empty at
the time — check current state with
`awk -F= '{print $1"=<"length($2)" chars>"}' .env` (never print the actual
value) rather than assuming either way; the user may have added a real key
since.

### Where else to find things

- **`CLAUDE.md`** — architecture, conventions, and accumulated gotchas
  (Gemini model deprecation, per-model free-tier quota, the
  `thought_signature` round-trip, raw-JSON SDK errors, the circular-import
  invariant, the `result.ok === false` trap, mandatory test-isolation env
  overrides). Read first for anything code-related.
- **`How to Use Jarvis.md`** — user-facing instructions; may be stale
  relative to the Round 2 task-popup changes above (Skills/Connectors
  wording) and worth a pass if the user asks for updated instructions.
