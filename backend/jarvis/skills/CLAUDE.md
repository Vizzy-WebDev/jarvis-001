# Skills (`jarvis/skills/`)

See the root `CLAUDE.md` for the PERMANENT RULE governing what may ever appear as a Skill
in the UI — it is cross-cutting (it constrains screens outside this directory) and stays
there on purpose. This file covers `jarvis/skills/` itself: **folder Skills only** — a
folder holding a `SKILL.md` under `data/skills/<name>/`. Real executable capabilities
(`get_weather`, `open_app`, `run_code`, ...) live in `jarvis/tools/`, not here.
`jarvis/capabilities/` merges tools, folder Skills and connector tools into one
declaration list and one dispatcher; this directory has no confirm gate of its own.

**The built-in-vs-Skill distinction is structurally enforced.** `capabilities/` tags every
entry `kind: builtin | skill | connector`, and `files.list_user_skills()` is the ONLY
function a Skills UI may call — it reads `data/skills/` folders directly and has no code
path that can return a built-in. Any future screen listing "things Jarvis can do" must be
built on a source that cannot structurally return a native ability, never on a filtered
capability list.

## Modules

- `files.py` — a leaf: a Skill folder on disk plus `data/skills.json`, which holds only
  per-Skill runtime state (`enabled`, `installedAt`, `source`,
  `scriptsApproved`, `pipelineApproved`) keyed by folder name. Never skill content — that
  lives entirely as files, so a folder can be copied in or out and still be the whole
  Skill. Parses and writes `SKILL.md` frontmatter, validates names (lowercase slug,
  Anthropic's naming rule) and resolves paths inside a Skill folder safely.
- `install.py` — getting a folder onto disk: `install_from_directory()`,
  `install_from_zip()`, `install_from_markdown()`, `install_from_github()` (resolves the
  repo's real default branch, downloads the zipball, then takes the same path as a local
  zip), `replace_contents()` and `export_zip()`. Zips are unpacked with the standard
  library's `zipfile`, and every entry's resolved destination is confirmed to be inside
  the target directory before anything is written, so a `../../.env` entry cannot
  escape. Wrapper folders (right-click zips, GitHub's `owner-repo-<sha>/`) are unwrapped.
- `pipelines.py` / `templates.py` — an optional `skill.toml`: a fixed, ordered pipeline of
  `tool` steps (dispatched through an injected executor, so every existing gate applies)
  and `prompt` steps (one model call, no tools). Parsed with `tomllib`; validation is
  strict and separate, because a mis-parsed pipeline EXECUTES. The executor is injected,
  never imported — reaching the capability seam from here would be a cycle.
- `capabilities.py` — `skill_spec()` / `sync()` turn each enabled Skill into a
  `CapabilitySpec` in the registry, and unregister ones that are gone. The shape depends on
  the folder: `SKILL.md` only → calling it returns the instructions; `skill.toml` → calling
  it RUNS the pipeline; an invalid or unapproved pipeline falls back to instructions with a
  note. Only frontmatter is read to build a declaration; the instruction body is read when
  the Skill is called (progressive disclosure). A pipeline whose steps need a human makes
  the whole Skill confirm once, up front. Skills are always visible (`core` tag) because a
  Skill the model doesn't know exists is one it never uses.

Neither `files.py` nor `install.py` imports the capability registry. The reverse
(`tools/*.py` → `skills/files.py`, or the registry loading `skills/capabilities.py`) is
fine and is how the merge happens.

## Scripts and pipelines need consent

**Installed content is instructions and files; code from elsewhere never runs by default.**
`tools/skill_tools.py` holds the built-in tools that act on a Skill folder:
`read_skill_file`, `run_skill_script`, `approve_skill_scripts`, `approve_skill_pipeline`
and `create_skill`. `run_skill_script` runs a helper script through the sandbox
(`jarvis/sandbox/CLAUDE.md`) and is gated by a one-time, per-Skill consent (mirroring
`tools/allow_folder.py`, not a per-call confirm that would re-ask every run): until
`scriptsApproved` is set the call fails with a plain explanation and no sandbox run. A
`skill.toml` that arrived from elsewhere starts unapproved (`pipelineApproved`) the same
way. `read_skill_file` is deliberately not `meta`, since `background` turns (scheduled
tasks) drop every meta tool and a Skill's supporting files must work on a schedule too.
`allow-tools` in a downloaded `SKILL.md` is advisory only — no Jarvis screen sets it.
