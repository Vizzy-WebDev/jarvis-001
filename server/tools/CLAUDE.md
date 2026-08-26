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
  summarize(args) { return '...' },    // optional — read-back text when confirm is set
  async run(args, ctx) { return {...} },
}
```

`parameters` is passed straight through as the tool schema for every adapter, no
per-provider translation. `run()` returns plain data; the model phrases the spoken
reply. `ctx` carries `{sessionId, modelId, lowConfidence, autoConfirm}`. To add a tool:
new file in `server/tools/`, nothing else to touch — `capabilities.js`'s
`getToolDeclarations()` already strips `confirm`/`meta`/`summarize` before anything
reaches a model.

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

**Meta tools** (`schedule_task`, `list_tasks`, `cancel_task`, `configure_briefing`,
`remember_about_me`, `open_section`, `forget_something`, `update_memory`,
`review_memories`, `checkpoint_memories`, `search_conversations`) only make sense in live
conversation — `meta: true` excludes them from `capabilities.js`'s `listCapabilities()`,
which the task-creation and briefing-source pickers use. `remember_about_me` through
`checkpoint_memories` are Memory's write/review path; `search_conversations` is a
separate, read-only capability (full-text search over every past conversation via
`chat-store.js`, not a Memory write) — see `server/memory/CLAUDE.md`.

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
