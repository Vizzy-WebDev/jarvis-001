# Monitoring (`jarvis/monitor/`)

"Watch for X, then act." `store.py` is the CRUD over `data/monitors.json`; `engine.py`
evaluates the checks. The model-facing tools that create and stop a watch are in
`tools/monitor_tools.py`.

- **Check kinds, cheapest first.** `file_exists`, `file_gone`, `file_size_stable` and
  `web_page_changed` work on any machine and cost no model call. `window_*`, `process_*`,
  `element_text_matches` and `screen_looks_like` need a desktop (`control.available()`);
  `validate()` refuses them with a plain reason where there is none, rather than accepting
  a watch that could never fire. Only `screen_looks_like` costs a model call.
- **State between ticks.** `evaluate()` returns `(triggered, new_state)` and the engine
  stores the state on the monitor. `file_size_stable` and `web_page_changed` never fire on
  the first reading, since one reading cannot tell "finished" from "not started".
- **The clock.** `engine.start()` runs `check_all()` every `TICK_SECONDS` (60), gated by
  `JARVIS_MONITOR` like every background subsystem. Watches live in `monitors.json`, so
  any still-`watching` entry is picked up again after a restart with nothing to restore.
- **Firing** publishes a notification carrying the monitor's `onTrigger` text.
- **UI.** An amber "Watching for…" bar in `frontend/app/page.tsx` shows while any watch
  is active, stacked below the red control bar when both are showing.
