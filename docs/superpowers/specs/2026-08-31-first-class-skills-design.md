# First-class Skills across live chat, Jobs, and briefing

Status: approved, ready for implementation plan
Date: 2026-08-31

## Problem

Jarvis's data model already separates three categories of capability —
built-in tool, folder Skill (`data/skills/<name>/`, a `SKILL.md` the user
wrote or installed), and connector tool — via a `kind: 'builtin'|'skill'|
'connector'` tag enforced structurally (`server/capabilities.js`,
`server/skills/CLAUDE.md`'s PERMANENT RULE). Folder Skills are already
`core: true`, so they're always declared to the model in ordinary live
chat — this part works.

Two things are missing, confirmed by reading the code rather than assumed:

1. **No instruction anywhere tells a model to prefer an installed Skill**
   when one plausibly matches what's being asked, over reasoning the task
   out from its own general knowledge. The closest existing text
   (`prompt.js`'s `SYSTEM_INSTRUCTION`, the `find_capability` paragraph) is
   about discovering *hidden, non-core* capabilities in general — it never
   singles out Skills as a distinct category worth preferring.
2. **Background Jobs and connector-restricted briefings can structurally
   lose access to Skills entirely**, not just fail to be nudged toward
   them:
   - `server/jobs/orchestrator.js`'s `buildToolsetForKind()` filters a
     `research`/`files`-kind job's tool list down to a hardcoded raw-tool
     array (`KIND_TOOL_NAMES`) that never contains a Skill name. A
     `generic`-kind job is unaffected (it gets the unfiltered capability
     list), but `research`/`files` jobs cannot call a Skill under any
     circumstances today, however well it matches the job's goal.
   - `server/scheduler/briefing.js`'s `composeBriefing()` sets
     `allowedTools = connectorToolNames(config)` — connector tool names
     only — when the briefing has selected connectors. This drops Skills
     (and every other core tool) from that turn. `server/scheduler.js`'s
     own `prompt`-action already does this correctly (core tool names +
     connector tool names, confirmed by reading it), so `briefing.js` is
     the one place that diverges from the codebase's own established
     pattern.

## Goals

- Every turn that can reasonably use a Skill — live chat, a background
  Job of any kind, a scheduled task, a briefing — sees the currently
  installed Skills and is told, in the system prompt, to prefer one when
  it genuinely matches.
- Fix this by extending what's already there (`skillsSection()`, the
  `kind` tag, `scheduler.js`'s existing core+connector pattern), not by
  introducing a parallel mechanism.
- Keep the guidance proportionate to Jarvis's voice-first, low-ceremony
  personality — strong guidance, not an absolute "you must" mandate.

## Non-goals

- No category/priority system among multiple Skills (e.g. "process
  before implementation"). Not justified for a 4-Skill catalog; revisit
  if the Skill count grows enough that ties become common.
- No change to how a Skill is declared to the model (`folderSkillToTool()`
  in `server/skills/index.js`) — its shape (zero/simple-argument tool,
  `core: true`) already works for this purpose.
- No change to `capabilities.js`'s confirm gate, `invoke()` dispatch, or
  the core/`unlocked` two-tier split for built-in tools/connectors — all
  confirmed correct as-is.
- No admission-time "which Skills apply to this job" model call (the
  alternative design considered and rejected in brainstorming) — with 4
  Skills total, always including them is cheap and consistent with why
  Skills are already `core: true` in live chat.

## Design

### 1. Policy layer — extend `prompt.js`'s `skillsSection()`

This function is already unconditionally appended into the shared
`stable` system-prompt block built by `systemInstructionParts()` —
confirmed live chat, Jobs (`worker.js`'s `driveOneTurn` calls `runTurn`
with `background: true`), scheduled tasks, and briefings (`addressed:
true`) all build their prompt through this one function, so a change here
reaches every subsystem for free with no per-caller wiring.

Add one guidance sentence to the non-empty branch's returned text, after
the existing skill list: when what's being asked plausibly matches an
installed Skill, call it rather than reasoning the task out from scratch
— a real match only, not a stretch, and a Skill is a different thing from
an ordinary built-in ability or a connected app (not a renamed version of
either). Keep the existing "none right now" branch's wording as-is — no
guidance is needed when there's nothing installed.

### 2. Jobs — `server/jobs/orchestrator.js`'s `buildToolsetForKind()`

Current (paraphrased): for `kind !== 'generic'`, `filtered = caps.filter(c
=> KIND_TOOL_NAMES[kind]?.includes(c.name))`.

Change: also include any capability whose `c.kind === 'skill'`,
unconditionally, regardless of job kind:

```js
const filtered =
  kind === 'generic'
    ? caps
    : caps.filter((c) => c.kind === 'skill' || KIND_TOOL_NAMES[kind]?.includes(c.name));
```

`kind: 'computer'` is unaffected — it never calls `buildToolsetForKind()`
at all (`driveComputerJob` bypasses the tool-calling loop entirely, per
`jobs/CLAUDE.md`).

### 3. Briefing — `server/scheduler/briefing.js`'s `composeBriefing()`

Current: `const allowedTools = connectorToolNames(config);`

Change to mirror `scheduler.js`'s own existing `prompt`-action pattern
exactly (core tool names, which already include Skills via `core: true`,
plus the selected connectors' tool names):

```js
const allowedTools = usingConnectors
  ? [...getToolDeclarations({ includeMeta: true }).map((d) => d.name), ...connectorToolNames(config)]
  : [];
```

(`usingConnectors` itself is still derived from `connectorToolNames(config).length > 0` — only the
composition of `allowedTools` changes; the no-connectors branch keeps
today's `noTools: true` behavior unchanged.) Needs `getToolDeclarations`
imported from `../capabilities.js` in `briefing.js` (not currently
imported there — `scheduler.js` already imports it for the same reason,
confirm the exact import path matches).

### 4. Discovery clarity — `capabilities.js`'s `searchCapabilities()` + `find_capability.js`

`searchCapabilities()`'s returned shape currently:
`{name, description, parameters}`. Add `kind: c.kind` (`'builtin'` or
`'connector'` in practice — Skills are `core: true` and structurally
excluded from this search already, per the existing `!c.core` filter, so
`'skill'` never appears here). `find_capability.js`'s `run()` mapping
(`matches.map(m => ({name, description}))`) passes `kind` through
unchanged. No behavior change — purely additive information for the
model to weigh a discovered match by category.

## Files touched

- `server/prompt.js` — `skillsSection()`
- `server/jobs/orchestrator.js` — `buildToolsetForKind()`
- `server/scheduler/briefing.js` — `composeBriefing()`, new import
- `server/capabilities.js` — `searchCapabilities()`
- `server/tools/find_capability.js` — `run()`'s match mapping

## Testing plan

All testing follows root `CLAUDE.md`'s discipline: scratch
`JARVIS_DATA_DIR`/`JARVIS_ENV_PATH`/`PORT`, never the user's real data; a
`node:http` stub model when live quota isn't needed for what's being
verified.

1. `node --check` on every changed file.
2. Direct, no-server verification: `node --input-type=module -e` scripts
   (with `JARVIS_DATA_DIR` pointed at a scratch dir containing one test
   Skill folder) calling `buildToolsetForKind('research')` and
   `('files')` directly, asserting the test Skill's name is present in
   the returned `allowedTools` (it must NOT have been present before this
   change — verify the failing case too, on the pre-fix code or via git
   stash, so the test is proven to actually catch the bug).
3. Same pattern for `skillsSection()`/`systemInstructionFor()` — assert
   the new guidance sentence and the test Skill's name both appear in the
   returned prompt text.
4. One real end-to-end run: scratch server + stub `node:http` model
   (canned response, captures the request body it received) + a test
   Skill folder + a `research`-kind job whose goal names something only
   the test Skill could plausibly satisfy. Confirm from the captured
   request that (a) the Skill's tool declaration was sent to the model,
   (b) the guidance sentence was in the system prompt, and — if the stub
   is scripted to emit a tool call for the Skill — that `capabilities.js`
   actually invoked it and the job's trace/transcript reflects a real
   result, not a refusal.
5. A parallel scratch check for `composeBriefing()` with one connector
   selected, confirming `allowedTools` now includes the test Skill's name
   alongside the connector's tool names.

## Open questions

None — all three brainstorming decisions (always-include-Skills for Jobs,
include briefing.js in scope, strong-guidance-not-mandate tone) were
made explicitly with the user before this spec was written.
