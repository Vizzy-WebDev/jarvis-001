# Tools (`server/tools/*.js`)

Real, executable JavaScript capabilities — `get_weather`, `open_app`, `run_code`,
`schedule_task`, ... — as distinct from a Skill (`server/skills/`), which is a folder of
*instructions* with no code of its own. See the root `CLAUDE.md`'s "Tools" / "Skills" /
"`server/capabilities.js`" sections for the PERMANENT RULE governing what may ever
appear as a Skill in the UI, and for why this split exists (this directory used to hold
both built-in tools and folder Skills at once, under the one word "skill" — the leak
that caused). This file covers the architecture of `server/tools/*.js` itself.

Auto-loaded by `tools/index.js`. Each file default-exports:

```js
{
  name, description, parameters: <JSON Schema>,
  confirm: 'always' | 'ifUnclear',    // optional — see "Voice-clarity confirmation" below
  meta: true,                          // optional — excludes it from the task/briefing skill picker
  internal: true,                      // optional — excludes it from listCapabilities()/listStepCandidates() WITHOUT
                                        // stripping it from a background:true turn's declarations the way meta does
  summarize(args) { return '...' },    // optional — read-back text when confirm is set
  async run(args, ctx) { return {...} },
}
```

`parameters` is passed straight through as the tool schema for every adapter, no
per-provider translation. `run()` returns plain data; the model phrases the spoken
reply. `ctx` carries `{sessionId, modelId, lowConfidence, autoConfirm, background,
onEscalate}` (`onEscalate`, see "The third confirm mode" below, is present only for a
background Job's own turn — undefined for every other caller). To add a tool: new file
in `server/tools/`, nothing else to touch — `capabilities.js`'s `getToolDeclarations()`
already strips `confirm`/`meta`/`internal`/`summarize` before anything reaches a model.

**`internal` is a different axis from `meta`, not a stronger version of it** — see
`report_job_done.js`/`report_job_stuck.js`/`request_job_split.js` (root `CLAUDE.md`'s
"Background Task Orchestration" section) for why both exist. `meta:true` is stripped by
`getToolDeclarations({includeMeta: !opts.background})` — i.e. a `background:true` turn
(every background Job's own turn included) never sees a meta tool at all, which is
exactly wrong for a tool a Job's OWN turn needs to call. `internal:true` instead stays
fully visible to `getToolDeclarations()` and is only ever excluded from the two
enumerations a human-facing picker is built on (`listCapabilities()`,
`listStepCandidates()`). An internal tool only actually reaches a model when its name
is explicitly named in that turn's `opts.allowedTools` — see the Model system section's
note on `allowedTools` now being *enforced*, not just offered.

**`tools/index.js` itself only loads and enumerates — it owns no confirm gate.**
`server/capabilities.js` is the seam that merges tools with folder Skills and connector
tools into one declaration list and owns `invoke()`, the one dispatcher. A tool file is
never called directly by anything outside `capabilities.js` except `control/session.js`,
which imports one specific leaf tool (`open_app.js`) directly — see the root
`CLAUDE.md`'s circular-import gotcha for why that's the one sanctioned exception.

**Voice-clarity confirmation** — a tool with `confirm: 'always'` (or `'ifUnclear'`,
gated on `ctx.lowConfidence`) doesn't run on first call: it returns
`{needs_confirmation, summary, confirm_token}`, the model reads the summary back and
waits for a yes — spoken or typed, both are just the user's next message — and only a
second call carrying that token actually runs it (see `capabilities.js`'s `pending`
Map). **The confirmed call runs with the ORIGINAL arguments captured when the token was
issued, never whatever the model resends alongside the token** — an earlier version
required the resent args to match the original byte-for-byte (a sorted-JSON equality
check) and broke silently for any tool with a complex/nested argument shape: the model
saying "yes, doing it now" with a slightly-regenerated args object failed the equality
check, minted a fresh token, and asked again — indistinguishable from an infinite
confirmation loop no matter how many times the user said yes. The token itself (random,
single-use, tool-name-scoped, 5-minute TTL) is the whole proof of consent now; nothing
about the resent args is trusted or even required — `prompt.js`'s instruction reflects
this (“call the tool again with confirm_token set… you don't need to reconstruct or
resend the original arguments”). `ctx.autoConfirm` bypasses this entirely — set only
by unattended callers (`scheduler.js`, `briefing.js`) that already got the user's
one-time consent at setup, since nobody's present to answer a live prompt on a schedule.

**`confirm_token` is a real, declared schema argument, not just prose — a confirmed,
live bug this used to be, not a hypothetical.** Every individual confirm-gated tool file
(15+ of them, plus a folder Skill's own pipeline-generated `confirm: 'always'` —
`skills/index.js`) still declares only its own real arguments in `parameters`; NONE of
them ever declared `confirm_token` itself. `capabilities.js`'s `getToolDeclarations()` —
the one place every capability's schema is actually built for a model to see — now
injects it (`withConfirmToken()`) as an optional string property onto any capability
whose `confirm` is set, so a model that sticks strictly to its own declared
function-calling schema (confirmed live to be common on weaker/free-tier models — see
root `CLAUDE.md`'s Self-Model section) has somewhere to actually put the token after
being told to send it back. Before this fix, such a model would say "yes" and then have
no schema-visible field to carry the token in — `consumePendingToken()` correctly found
nothing, `requiresConfirmation()` fired again, and the exchange looked exactly like an
infinite confirmation loop, indistinguishable from the byte-for-byte-comparison bug this
same paragraph already describes above, but from a different cause entirely. Fixed once,
centrally, rather than in every tool file — new confirm-gated tools get this for free.

**A token may only be redeemed in a LATER turn than the one that minted it — never the
same one, found live in the very next round of testing.** Fixing the schema gap above
removed an ACCIDENTAL protection along with the real bug: before it, a confirm-gated tool
was structurally incapable of ever completing in one turn (no field to carry the token),
which happened to also block a model from completing an entire ask-and-answer round trip
on its own. Once the schema carried a real `confirm_token`, a model told "skip asking me"
could mint the token and immediately resend it in the SAME turn — confirmed live: a real
free-tier model, told to forget a real memory "without asking to confirm," minted a token
and redeemed it five seconds later in the same turn, deleting the memory with no separate
human reply ever happening. `capabilities.js`'s `consumePendingToken()` now refuses a
token whose `mintedTurnId` matches the redeeming call's own `ctx.turnId`
(`models/runner.js`'s `runTurn()` mints one id per turn, threaded through every step and
every candidate-model retry of that turn) — the SAME as an invalid token: it just asks
again, and only a genuinely later turn (a real new message) can complete it. Only
enforced when both sides carry a real `turnId`; a caller outside the per-turn system
(unattended `autoConfirm`/`onEscalate` callers never reach this code path at all) is
unaffected.

**Meta tools** (`schedule_task`, `list_tasks`, `cancel_task`, `configure_briefing`,
`remember_about_me`, `open_section`, `forget_something`, `update_memory`,
`review_memories`, `checkpoint_memories`, `search_conversations`, `record_lesson`,
`review_improvements`, `check_myself`, `track_goal`) only make sense in live
conversation — `meta: true` excludes them from `capabilities.js`'s `listCapabilities()`,
which the task-creation and briefing-source pickers use. `remember_about_me` through
`checkpoint_memories` are Memory's write/review path; `search_conversations` is a
separate, read-only capability (full-text search over every past conversation via
`chat-store.js`, not a Memory write) — see `server/memory/CLAUDE.md`.
`record_lesson`/`review_improvements` are Self-Improvement's own live-conversation-only
pair (filing something the user just plainly taught, and answering "what have you
learned/changed") — see `server/improvement/CLAUDE.md`. `suggest_improvement`/
`undo_improvement`, the other two Self-Improvement tools, are deliberately NOT meta
(reachable via `find_capability` like any other non-core tool) — proposing an idea or
undoing a change is rare enough not to need an always-declared slot the way the two
above do. `check_myself`/`track_goal` are the Self-Model's own pull/write pair (real,
evidence-backed self-knowledge, and the model's own record of what it understands a
live conversation's goal to be) — see `server/self/CLAUDE.md`; a job already has its own
durable `goal` column (`jobs/job-store.js`) and never needs `track_goal`.

**Check a tool result with `result.ok === false`, never `!result.ok`, when
testing for failure** — `get_time` (and any tool with nothing to report
beyond success) returns no `ok` field at all on success; a bare `!result.ok`
check silently treats that as a failure.

**Tools that touch the OS use an allowlist, never raw shell strings.** See
`open_app.js` (friendly name -> fixed command map, AI text never reaches the target
directly) and `open_website.js` (`new URL()` validation, `http`/`https` only). Both
launch via `spawn('cmd.exe', ['/c', 'start', '', target], ...)` — array args, no string
interpolation into a shell command.

**Four files here still import from `server/skills/store/skill-files.js`** —
`create_skill.js`, `read_skill_file.js`, `run_skill_script.js`, `approve_skill_scripts.js`
— because they read/write folder-Skill state (creating one, reading a skill's
supporting file, running/approving one of its scripts) even though they are themselves
built-in tools, not Skills. That's a legitimate, one-directional dependency
(`server/tools/` -> `server/skills/store/`, a dependency-free leaf, never the loader) —
it does not cross the circular-import invariant.

**`create_skill.js` needs `capabilities.js`'s `reservedSkillNames()`, and cannot import
it directly** — it's dynamically imported BY `tools/index.js`'s loader loop at startup,
so a top-level import back to `capabilities.js` (which itself imports `tools/index.js`)
would deadlock. `capabilities.js`'s `invoke()` injects `ctx.reservedSkillNames` for
exactly this reason.
