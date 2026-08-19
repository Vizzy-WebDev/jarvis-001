# Monitoring (`server/monitor/`)

"Watch for X, then act": `monitor-store.js` (CRUD) + `engine.js` (the checks), driven by
`skills/watch_for.js` (translates a natural-language ask into a check) and
`skills/stop_watching.js`. Check kinds are cheapest-first — `window_appears`/
`window_gone`, `file_exists`/`file_size_stable` (a real two-tick lifecycle: a baseline
tick, then a stable-size tick, via `startWatching()`'s own timers) need no model call at
all; anything needing judgment does. An amber "Watching for…" bar (`app.js`/`style.css`)
shows while active, stacking with the red control bar rather than overlapping it (though
it shares that bar's pre-existing header-overlap layout issue — both are
`position: fixed; top: 0` with nothing compensating in header padding), and
`resumeActiveMonitors()` restores any still-`watching` entry on server restart.
