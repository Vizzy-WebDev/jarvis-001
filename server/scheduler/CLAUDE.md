# Scheduler + briefing (`server/scheduler/*.js`)

- `recurrence.js` — pure `nextRunAt(spec, from)`/`describe(spec)` over `{type: 'once'|
  'daily'|'weekdays'|'weekly'|'interval', ...}`. No state, no I/O — test it directly.
- `task-store.js` — plain CRUD over `data/tasks.json`/`task-runs.json`. Deliberately a
  leaf module (see circular-import gotcha in the root `CLAUDE.md`).
- `scheduler.js` — the 30s tick + `runAction()`. A task's action is `message` (fixed
  text, no model involved), `skill` (calls the chosen capability directly — a built-in
  tool or a folder Skill, not via a model's own judgment, since the user already picked
  it — then has a model narrate the result), `prompt` (free-text through the model), or
  `briefing`. Catches up a missed run once (never once-per-missed-day) and flags it
  `late`. Every unattended run records which model handled it and any fallback, via
  `runner.js`'s `model_switch` events. A `prompt` action's `connectors` field (an array
  of connector ids, from the task-creation UI's real picker — see
  `public/screens/tasks.js`) is resolved to real tool names fresh at RUN time (a
  connector's own tool list can change between creation and run) via
  `connectors/index.js`'s `toolNamesForConnector()`, and ADDS those names to the task's
  normal core built-in set rather than restricting the task down to only them — empty
  (the default) means fully unrestricted, unchanged from before this picker existed.
  **Verification (root CLAUDE.md's Operational Awareness item 4)** — for a `prompt`
  action's result reporting `ok:true`, `runTaskNow()` runs `ops/verify.js`'s
  `verifySemanticMatch({request: task.action.text, resultSummary: result.summary})`
  before `recordRun()`. Only `prompt` actions get this — `message`/`briefing`/`skill`
  results are mechanical or already structured, nothing free-form to mismatch. A
  `matches:false` verdict flips `result.ok` to `false` and folds the reason into
  `result.error`, letting the EXISTING `notify`/`recordRun` machinery treat it exactly
  like any other failure — **no new recovery mechanism**, since a task's own next
  scheduled occurrence already is its natural retry cadence (unlike Jobs, there's no
  existing retry loop here to reuse or collide with). `checked:false` (no model
  available) never flips a real success to a failure. Verified via a real stub model
  through the real `runTaskNow()` path: a reply that doesn't answer the actual prompt
  is correctly flagged `ok:false` with the real reason in `error` (the original reply
  text stays in `summary`, never lost); a genuinely matching reply is untouched.
- `briefing-config.js` / `briefing.js` — split for the same circular-import reason as
  task-store.js. Sections (greeting, date/time, upcoming tasks, goals, focus, custom)
  are fixed; weather/headlines are fixed, always-available native abilities, not a
  user-managed list (an earlier open `sources[]` shape was reverted — it let Jarvis's
  own built-ins be offered through the same "add a source" UI as a Skill, a real
  instance of the native-ability-as-Skill bug). Connectors are the one real, user-picked
  addition (`config.connectors`, an array of connector ids, empty by default — see the
  Briefing screen's own picker): `composeBriefing()` still gathers everything else in
  code first and narrates only what it found (never lets the model invent data), but
  when at least one connector is selected the turn ALSO gets real tool access to
  exactly those connectors' tools, on top of the code-gathered facts, so the model can
  genuinely check them rather than only narrate pre-fetched data. Every enabled folder
  Skill is added to that same tool list unconditionally too (`listUserSkills()`), even
  with no connectors selected at all — deliberately narrower than `scheduler.js`'s own
  prompt-action pattern (which widens to the full core built-in set): a briefing stays a
  narrate-code-gathered-facts turn, only gaining a Skill and whichever connectors were
  explicitly picked, never the rest of Jarvis's abilities.
