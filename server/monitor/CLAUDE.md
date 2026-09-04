# Monitoring (`server/monitor/`)

"Watch for X, then act": `monitor-store.js` (CRUD) + `engine.js` (the checks), driven by
`skills/watch_for.js` (translates a natural-language ask into a check) and
`skills/stop_watching.js`. Check kinds are cheapest-first — `window_appears`/
`window_gone`, `file_exists`/`file_size_stable` (a real two-tick lifecycle: a baseline
tick, then a stable-size tick, via `startWatching()`'s own timers) need no model call at
all; anything needing judgment does. An amber "Watching for…" bar (`app.js`/`style.css`)
shows while active, stacking with the red control bar rather than overlapping it —
**checked directly against the real CSS/DOM during a later refinement pass: this already
works correctly** (a sibling-selector rule shifts the amber bar down by the red bar's
height when both are showing, and the observation dot shifts further still when both are
active), correcting an earlier version of this note that called it a known bug. The two
DO still both sit above the app's own header buttons while showing (a separate, minor,
purely cosmetic detail in a part of the layout math this project's own comments already
flag as previously fragile — left alone on purpose rather than risking a regression there
for something cosmetic). `resumeActiveMonitors()` restores any still-`watching` entry on
server restart.
