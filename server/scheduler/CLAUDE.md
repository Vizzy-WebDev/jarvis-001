# Scheduler + briefing (`server/scheduler/*.js`)

- `recurrence.js` — pure `nextRunAt(spec, from)`/`describe(spec)` over `{type: 'once'|
  'daily'|'weekdays'|'weekly'|'interval', ...}`. No state, no I/O — test it directly.
- `task-store.js` — plain CRUD over `data/tasks.json`/`task-runs.json`. Deliberately a
  leaf module (see circular-import gotcha in the root `CLAUDE.md`).
- `scheduler.js` — the 30s tick + `runAction()`. A task's action is `message` (fixed
  text, no model involved), `skill` (calls the chosen Skill directly — not via a model's
  own judgment, since the user already picked it — then has a model narrate the
  result), `prompt` (free-text through the model), or `briefing`. Catches up a missed
  run once (never once-per-missed-day) and flags it `late`. Every unattended run
  records which model handled it and any fallback, via `runner.js`'s `model_switch`
  events.
- `briefing-config.js` / `briefing.js` — split for the same circular-import reason as
  task-store.js. Sections (greeting, date/time, upcoming tasks, goals, focus, custom)
  are fixed; live data (weather, headlines, anything else) is an open `sources` array,
  each `{skillName, args, enabled, label}` — `briefing.js` calls `runSkill()`
  generically per source, then has a model narrate the gathered facts. Never lets the
  model invent data.
