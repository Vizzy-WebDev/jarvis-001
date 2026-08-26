# Skills (`server/skills/*.js`)

See the root `CLAUDE.md`'s "Skills" section for the PERMANENT RULE governing what may
ever appear as a Skill in the UI — that rule is cross-cutting (it constrains other
screens outside this directory) and stays there on purpose. This file covers the
architecture of `server/skills/*.js` itself: **folder Skills only** — a folder of
`SKILL.md` instructions under `data/skills/<name>/`. Real executable capabilities
(`get_weather`, `open_app`, `run_code`, ...) live in `server/tools/` — see
`server/tools/CLAUDE.md` — not here. `server/capabilities.js` is the seam that merges
tools, folder Skills (this directory), and connector tools into one declaration list and
one dispatcher (`invoke()`); this directory has no confirm gate of its own.

**The built-in-vs-Skill distinction is structurally enforced, not just
remembered.** `capabilities.js`'s `listCapabilities()` tags every entry
`kind: 'builtin' | 'skill' | 'connector'`, and `listUserSkills()` is the
**only** function any Skills UI may call — it reads `data/skills/` folders
directly and has no code path that can ever return a built-in. **When adding
any future screen that lists "things Jarvis can do," it must be built on a
source that cannot structurally return a native ability — never on a filtered
`listCapabilities()`/`getToolDeclarations()`.**

`server/skills/index.js` itself is small: it owns exactly one thing, turning an enabled
folder Skill into a runnable, zero-argument tool object
(`listFolderSkillTools()`/`getFolderSkillTool()`), which `capabilities.js` merges in
alongside tools and connectors. It does not load built-in tools, does not merge
connectors, and does not own the confirm gate.

**Folder Skills** (`server/skills/store/{skill-files.js,skill-zip.js}`, `data/skills/<name>/`)
— a folder holding a `SKILL.md` (YAML-ish frontmatter + instructions) plus any
supporting files it needs — the same layout Claude Code and skills.sh use, so a folder
from either drops in unchanged. Pure instructions and reference material, written by the
user (in-app, or by "Have Jarvis write it" — one `askModel` call drafting a `SKILL.md`
the user reviews before saving), or imported from a folder/`.zip` already on the user's
PC — **never downloaded code that runs**. Each enabled one is merged into
`capabilities.js`'s `getToolDeclarations()`/`listCapabilities()`/`invoke()` as an
ordinary zero-argument tool (`folderSkillToTool()`, `server/skills/index.js`); the
declaration list only reads a folder's frontmatter (`listSkillFolders()` — cheap, no
body), and the full instructions are read (`readSkillBody()`) only when the tool is
actually called — the same progressive-disclosure shape Claude Code itself uses.
Calling it returns its instructions plus a list of any other files in its folder and a
note that `read_skill_file` (`server/tools/read_skill_file.js`) is how to open them —
settings filled in once (a `settings.json` sidecar, JSON Schema) get appended as a plain-
language note too. `allowed-tools` (from a downloaded `SKILL.md`'s own frontmatter —
**no Jarvis screen offers to set this**; it's read-only support for a skill written
elsewhere) is advisory only — communicated to the model as a note, not hard-enforced by
filtering the live tool list mid-turn.

**Jarvis never executes a script inside a Skill folder — only reads it, like any other
file.** Deliberate: scheduled tasks and the briefing run with `ctx.autoConfirm` on (see
`server/tools/CLAUDE.md`'s Voice-clarity confirmation section), so "run it, but ask
first" would silently skip asking on an unattended run. If real execution is ever
needed, it's a separate, deliberately-designed feature — not a default this build opened
the door to. Correspondingly, `read_skill_file` (in `server/tools/`, since it's a
built-in tool, not a Skill itself) is **not** `meta: true` even though it's an internal
helper — `background: true` turns (scheduled tasks) drop every meta tool, and a Skill's
supporting files need to work on a schedule just as much as live; the one cost is it
also shows up as a pickable (if odd) task/briefing action, which is harmless.

Neither store file imports `tools/index.js` or `capabilities.js` (same leaf-module
reasoning as `task-store.js` in `server/scheduler/CLAUDE.md`) — the reverse import
(`server/tools/*.js` -> `server/skills/store/...`, or `capabilities.js` ->
`server/skills/index.js`) is fine and is how the merge happens. `data/skills.json` now
holds only small per-skill runtime state (`enabled`, `settingsValues`, `installedAt`,
`source`, `scriptsApproved`) keyed by folder name — never skill content, which lives
entirely as files under `data/skills/`.

**Script execution — the one deliberate exception to "Jarvis only ever reads a Skill's
files, never runs them."** `server/tools/run_skill_script.js` runs a Skill's own helper
script through the sandbox (see `server/sandbox/CLAUDE.md`); `server/tools/approve_skill_scripts.js`
is a one-time, per-Skill consent gate — mirrors `server/tools/allow_folder.js`'s shape
rather than the generic per-call `confirm:'always'` gate (which would re-ask on every
run): the first `run_skill_script` call for a Skill fails with a plain explanation and
no sandbox run at all until the user agrees and `scriptsApproved` is set; every later
call for that Skill then runs immediately with no further prompt. Both files live in
`server/tools/` (they're built-in code, not Skill content) even though their whole job is
to act on a Skill folder — see `server/tools/CLAUDE.md`'s note on the one-directional
`server/tools/` -> `server/skills/store/` dependency this creates.
