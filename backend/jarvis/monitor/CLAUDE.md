<!-- Ported from the Node build during the S6 cutover. The architecture, the
invariants and the live-caught bugs described here all carried over deliberately and
still hold. File paths have been updated to their real Python counterparts and are
verified to exist. Function names written in camelCase (`getToolDeclarations()`) are
the NODE originals, kept because the surrounding reasoning is about them; the Python
equivalent is the snake_case function doing that job in the same module. Where a Node
module had no Python counterpart, the text says so rather than pointing at a file that
does not exist. -->

# Monitoring (`jarvis/monitor/`)

"Watch for X, then act": `monitor/store.py` (CRUD) + `engine.py` (the checks), driven by
`tools/monitor_tools.py` (translates a natural-language ask into a check) and
`tools/monitor_tools.py`. Check kinds are cheapest-first — `window_appears`/
`window_gone`, `file_exists`/`file_size_stable` (a real two-tick lifecycle: a baseline
tick, then a stable-size tick, via `startWatching()`'s own timers) need no model call at
all; anything needing judgment does. An amber "Watching for…" bar (`frontend/app/page.tsx`/`style.css`)
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
