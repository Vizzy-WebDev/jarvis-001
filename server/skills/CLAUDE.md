# Skills (`server/skills/*.js`)

See the root `CLAUDE.md`'s "Skills" section for the PERMANENT RULE governing what may
ever appear as a Skill in the UI — that rule is cross-cutting (it constrains other
screens outside this directory) and stays there on purpose. This file covers the
architecture of `server/skills/*.js` itself.

**The built-in-vs-Skill distinction is structurally enforced, not just
remembered.** `listSkills()` used to flatten built-in code abilities, user
Skill folders, and connector tools into one array with no marker of which
kind each entry was — any screen consuming that list was physically unable to
tell them apart. Every entry now carries an explicit
`kind: 'builtin' | 'skill' | 'connector'`, and `listUserSkills()` is the
**only** function any Skills UI may call — it reads `data/skills/` folders
directly and has no code path that can ever return a built-in. **When adding
any future screen that lists "things Jarvis can do," it must be built on a
source that cannot structurally return a native ability — never on a filtered
`listSkills()`/`getToolDeclarations()`.**

Auto-loaded by `skills/index.js`. Each file default-exports:

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
reply. `ctx` carries `{sessionId, modelId, lowConfidence, autoConfirm}`. To add a skill:
new file in `server/skills/`, nothing else to touch — `getToolDeclarations()` already
strips `confirm`/`meta`/`summarize` before anything reaches a model.

**Voice-clarity confirmation** — a skill with `confirm: 'always'` (or `'ifUnclear'`,
gated on `ctx.lowConfidence`) doesn't run on first call: it returns
`{needs_confirmation, summary, confirm_token}`, the model reads the summary back and
waits for a yes — spoken or typed, both are just the user's next message — and only a
second call carrying that token actually runs it (see `skills/index.js`'s `pending`
Map). **The confirmed call runs with the ORIGINAL arguments captured when the token was
issued, never whatever the model resends alongside the token** — an earlier version
required the resent args to match the original byte-for-byte (a sorted-JSON equality
check) and broke silently for any skill with a complex/nested argument shape: the model
saying "yes, doing it now" with a slightly-regenerated args object failed the equality
check, minted a fresh token, and asked again — indistinguishable from an infinite
confirmation loop no matter how many times the user said yes. The token itself (random,
single-use, skill-name-scoped, 5-minute TTL) is the whole proof of consent now; nothing
about the resent args is trusted or even required — `prompt.js`'s instruction reflects
this (“call the tool again with confirm_token set… you don't need to reconstruct or
resend the original arguments”). `ctx.autoConfirm` bypasses this entirely — set only
by unattended callers (`scheduler.js`, `briefing.js`) that already got the user's
one-time consent at setup, since nobody's present to answer a live prompt on a schedule.

**Meta skills** (`schedule_task`, `list_tasks`, `cancel_task`, `configure_briefing`,
`remember_about_me`, `open_section`, `forget_something`, `update_memory`,
`review_memories`, `checkpoint_memories`) only make sense in live conversation —
`meta: true` excludes them from `listSkills()`, which the task-creation and
briefing-source pickers use. The last four are Memory's write/review path — see
`server/memory/CLAUDE.md`.

**Check a skill result with `result.ok === false`, never `!result.ok`, when
testing for failure** — `get_time` (and any skill with nothing to report
beyond success) returns no `ok` field at all on success; a bare `!result.ok`
check silently treats that as a failure.

**Skills that touch the OS use an allowlist, never raw shell strings.** See
`open_app.js` (friendly name -> fixed command map, AI text never reaches the target
directly) and `open_website.js` (`new URL()` validation, `http`/`https` only). Both
launch via `spawn('cmd.exe', ['/c', 'start', '', target], ...)` — array args, no string
interpolation into a shell command.

**Folder Skills** (`server/skills/store/{skill-files.js,skill-zip.js}`, `data/skills/<name>/`) — a second kind of Skill
alongside the built-in code files above: a folder holding a `SKILL.md` (YAML-ish
frontmatter + instructions) plus any supporting files it needs — the same layout Claude
Code and skills.sh use, so a folder from either drops in unchanged. Pure instructions and
reference material, written by the user (in-app, or by "Have Jarvis write it" — one
`askModel` call drafting a `SKILL.md` the user reviews before saving), or imported from a
folder/`.zip` already on the user's PC — **never downloaded code that runs**. Each
enabled one is merged into `skills/index.js`'s `getToolDeclarations()`/`listSkills()`/
`runSkill()` as an ordinary zero-argument tool (`folderSkillToTool()`); the declaration
list only reads a folder's frontmatter (`listSkillFolders()` — cheap, no body), and the
full instructions are read (`readSkillBody()`) only when the tool is actually called —
the same progressive-disclosure shape Claude Code itself uses. Calling it returns its
instructions plus a list of any other files in its folder and a note that
`read_skill_file` (`server/skills/read_skill_file.js`) is how to open them — settings
filled in once (a `settings.json` sidecar, JSON Schema) get appended as a plain-language
note too. `allowed-tools` (from a downloaded `SKILL.md`'s own frontmatter — **no Jarvis
screen offers to set this**; it's read-only support for a skill written elsewhere) is
advisory only — communicated to the model as a note, not hard-enforced by filtering the
live tool list mid-turn.

**Jarvis never executes a script inside a Skill folder — only reads it, like any other
file.** Deliberate: scheduled tasks and the briefing run with `ctx.autoConfirm` on (see
above), so "run it, but ask first" would silently skip asking on an unattended run. If
real execution is ever needed, it's a separate, deliberately-designed feature — not a
default this build opened the door to. Correspondingly, `read_skill_file` is **not**
`meta: true` even though it's an internal helper — `background: true` turns (scheduled
tasks) drop every meta tool, and a Skill's supporting files need to work on a schedule
just as much as live; the one cost is it also shows up as a pickable (if odd) task/
briefing action, which is harmless.

Neither store file imports `skills/index.js` (same leaf-module reasoning as `task-store.js`
in `server/scheduler/CLAUDE.md`) — the reverse import (`skills/index.js` → `skills/store/...`) is fine and is how
the merge happens. `data/skills.json` now holds only small per-skill runtime state
(`enabled`, `settingsValues`, `installedAt`, `source`, `scriptsApproved`) keyed by folder
name — never skill content, which lives entirely as files under `data/skills/`.

**Script execution — the one deliberate exception to "Jarvis only ever reads a Skill's
files, never runs them."** `server/skills/run_skill_script.js` runs a Skill's own helper
script through the sandbox (see `server/sandbox/CLAUDE.md`); `approve_skill_scripts.js` is a
one-time, per-Skill consent gate — mirrors `allow_folder.js`'s shape rather than the
generic per-call `confirm:'always'` gate (which would re-ask on every run): the first
`run_skill_script` call for a Skill fails with a plain explanation and no sandbox run at
all until the user agrees and `scriptsApproved` is set; every later call for that Skill
then runs immediately with no further prompt.
