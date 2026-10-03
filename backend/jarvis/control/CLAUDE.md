# Computer control (`jarvis/control/`)

Lets Jarvis operate the desktop — click, type, read windows, launch apps — toward a stated goal.
Deliberately **not** built on `orchestrator/pipeline.py`'s chat loop: a control session is
long-running, carries a fresh view of the screen every step, and must never be able to reach the
chat tool catalogue (rescheduling itself mid-click is not something a desktop task should be able
to do). It has its own loop and its own small fixed action set.

**Nothing in this package imports the tool loader, the capability registry or the orchestrator.**
The import graph is what keeps the action set separate, not a comment. (`session.py` reaches the
model only through `orchestrator/model_port.py`'s port.)

## Modules

- **`desktop.py`** — the desktop itself: windows, mouse, keyboard, screen. One seam, two
  implementations chosen by a probe (`backend()` is `"windows"` or `"none"`, like
  `sandbox/runner.py`): `WindowsDesktop` uses `pywinauto` and `pyautogui` (both Windows-only
  dependencies in `pyproject.toml`), and `NoDesktop` refuses in plain language on a headless
  machine. `available()` and `describe_desktop()` are what tools and the monitor check.
  `pick_window()` and `is_jarvis_own_window()` live here. Each method exists to prevent a specific
  known failure, so keep them small and one-purpose:
  - **Focus before typing, always.** A synthetic click does not reliably move keyboard focus
    (Windows' anti-focus-stealing rules can suppress it), so `type` and `key` call `focus()` on
    their target first and never rely on a click.
  - **Close means `WM_CLOSE`, never killing a process.** Windows 11's Notepad can share one
    process across several windows, so terminating "the" process can close someone else's
    unsaved document. Never kill a shared-host app to clean up a test window.
  - **The cursor glides** (`GLIDE_SECONDS`, ~250ms eased) and lands EXACTLY on target, because
    the mouse-drift stop guard compares the real cursor against where Jarvis put it.
  - **Scrolling works both ways** — never coerce the delta's sign away.
  - **Jarvis's own window is never a candidate.** The user usually talks to Jarvis through a
    browser tab, so the foreground window is often Jarvis itself. `is_jarvis_own_window()` is a
    known browser process with a title exactly "Jarvis" or starting with "Jarvis -"; `pick_window()`
    excludes it and falls back to it only if it is the only window open. `session.py`'s perceive
    step and `tools/look_at_screen.py` both go through it.
- **`session.py`** — the loop: PLAN (`prepare_plan()`, one model call shown to the user for
  approval) → repeat PERCEIVE (free: the window list plus the front window's UI tree, or a
  vision-gated screenshot fallback) → DECIDE (one model call, `role="control"`, against
  `CONTROL_TOOLS`: `perform_actions`, `report_done`, `report_stuck`, plus the enabled connectors'
  tools) → GUARD (`guard.py`, against the window the action actually targets) → ACT → back to
  PERCEIVE, until done, stuck, stopped, failed or `MAX_STEPS` (25). Action kinds: `launch_app`,
  `switch_window`, `minimize_window`, `restore_window`, `arrange_window`, `close_window`,
  `click`, `double_click`, `right_click`, `type`, `key`, `scroll`, `wait`.
  - **Looking again IS the verification.** There is no separate "did that work" step: the next
    perceive shows the result, and the model decides whether to continue, retry or finish. That is
    why batches are small (`MAX_ACTIONS_PER_BATCH` 4) and only `HISTORY_STEPS` (6) observations are
    kept. A batched call reports EVERY action, including ones left unattempted after a
    window-changing action, so "never ran" is never read as "succeeded".
  - **A risky action goes through the ordinary approvals seam** — the same persisted
    `approvals` row, `POST /api/approvals/{id}` and same-turn refusal as everything else
    (`policy/approvals.py`), not an in-memory promise. It waits at most `CONFIRM_TIMEOUT_S`
    (10 minutes); a timeout resolves as a decline. `is_waiting_for()` lets the approvals route answer
    a paused session directly.
  - `launch_app` polls up to 5 seconds (every 0.3s) for the new window to appear, because a
    cold-starting app can still be missing from the next perceive, which would read as "the launch
    failed" and open a second copy.
  - It tracks `pre_existing_handles` vs `created_handles` and only auto-closes windows Jarvis
    opened itself, at `report_done`; closing anything pre-existing or unsaved-looking always asks.
  - **Connector declarations are stripped before they reach a model.** Internally they carry
    bookkeeping fields (`connectorId`, risk); sending those made Gemini reject the ENTIRE request
    with a 400, on every DECIDE call, any time a connector was enabled — which is effectively always,
    since `browser` and `files` auto-register. `_connector_tools()` builds a model-facing array of
    `{name, description, parameters}` only, and keeps the full specs for dispatch. Two shapes for two
    audiences, as the chat path does. This is a malformed REQUEST, so falling back to another
    candidate model would not help.
  - `active()`, `status()`, `request_stop()`, `start()` and the status constants (`PLANNING`,
    `RUNNING`, `AWAITING_CONFIRMATION`, `DONE`, `STUCK`, `STOPPED`, `FAILED`) are its public surface.
- **`guard.py`** — two questions before every action. `classify_action_risk()` returns **safe**
  (look, focus, scroll), **notable** (click, type, launch — visible and reversible, so shown as it
  happens rather than gated) or **risky** (delete, send, pay, install, anything irreversible — a real
  confirmation). `check_blocklist()` checks the window title, process or URL against `safety.py`
  (`BLOCKED`). `evaluate()` combines them. **The word lists live in `connectors/risk.py` and are
  imported, not copied**, so there is one vocabulary and one place to fix.
  - The keyword scan runs BEFORE the kind lookup, so "press Enter" whose stated reason is "delete this
    permanently" is risky, not waved through as a keypress. An unknown kind is notable, never safe.
  - **A `type` action's label is the literal text being typed**, so it is checked only against
    dangerous command fragments (`rm -rf`, `format c:`, `drop table`, ...), not the everyday-language
    list — ordinary sentences are full of "write", "buy", "update" and "post".
  - **Identifiers keep their casing into the tokenizer; prose is lowercased first.** Lowercasing an
    identifier before splitting its camelCase boundaries destroys them (a tool named
    `COMPOSIO_MULTI_EXECUTE_TOOL` was always classified risky because "execute" was baked in), while
    running the camelCase split over natural language finds false boundaries in proper nouns
    ("SharePoint" → "Share" + "Point" → matches "share"). Two text shapes, two rules.
  - **A generic "run whatever tool was found" dispatcher** (a gateway-style MCP server such as
    Composio's) is a structural blind spot for any static per-declaration classifier: its real danger
    is in its arguments, which are never seen. Such a dispatcher is uniformly "notable". Per-call,
    argument-aware classification is the honest fix if that gap ever needs closing.
- **`safety.py`** — the configured blocklist and capture retention (`GET/POST /api/control/safety`).
  Merge-over-defaults like `prefs.py`. The defaults are NOT empty: banking sites, well-known
  password managers and Windows' own security prompts are blocked from the first run. A list the user
  saved REPLACES the default, so removing an entry really removes it.
- **`overlay.py`** — the always-on-top red control bar, a separate process (so a stop button is not
  blocked by the thing being stopped), built with the standard library's `tkinter`. **Three stops, all
  ending the session rather than pausing it:** the overlay's button, the global hotkey `Ctrl+Alt+J`
  (polled, not registered — a registered hotkey can be refused if something owns the combination, and
  failing silently is what a stop must never do), and moving your own mouse (the cursor is compared
  before every synthetic mouse action, within `CURSOR_DRIFT_TOLERANCE`).
- **`watching.py`** — the "Jarvis can see your screen" badge and Screen Sharing. An **observation**
  is one capture for one question: `observe()` is a context manager and observations are counted
  (tokens), since several can overlap (a glance while a monitor watches). **Screen sharing** is a MODE
  the user turns on and leaves on; it arms the badge and tells the model, through a prompt section
  gated on `is_sharing()`, that the next screen question needs no "look at my screen" first. Turning
  it on never describes anything by itself. `routes/control.py`'s `/api/observation/share/start`,
  `/stop` and `/observation/stop` and the voice-facing `tools/screen_sharing.py` (`share_screen`,
  `stop_sharing_screen`) all land here, so a spoken instruction and the UI toggle cannot disagree.
  State lives in the process, not on disk: "am I looking right now" must be false after a restart.
- **`recorder.py`** — real screen video via ffmpeg (`gdigrab`, `libx264`, `yuv420p`, `+faststart`,
  which is what makes it playable in a `<video>` element). ffmpeg is resolved on PATH and common
  install locations, never bundled; without it the answer is a plain-language explanation
  (`NO_FFMPEG`). It is stopped by writing `q` to ffmpeg's stdin so the container is finalised; killing
  it leaves a file that exists, has a plausible size and does not play. Independent of the control
  loop — a recording keeps running through a session.
- **`captures.py`** — where screenshots and recordings live and when they expire, for both kinds. Its
  directory comes from `store.data_dir()` like every other data path, with a dedicated variable only as
  an explicit override on top; a module that derives its own path from its source location writes into
  the user's REAL data folder even under a scratch `JARVIS_DATA_DIR`. Ids are opaque and generated
  here, so nothing a model wrote ever becomes part of a filename; retention comes from `safety.py`
  (`screenshotRetention`, `recordingRetention`).
- **`selfcheck.py`** — `python -m jarvis.control.selfcheck`. The loop, guard, approvals and tools are
  tested against a fake desktop, but the last inch of hardware cannot be. This opens a scratch Notepad,
  does one real thing at a time, checks the real OS and prints pass/fail per behaviour. It touches none
  of the user's data.

## Tools and routes

`tools/control_computer.py` — **two calls, always.** The first returns a plan and takes over nothing;
the user reads it and agrees; the second, carrying `confirmed`, starts the session. It is HIGH risk, so
a scheduled task or briefing can never start one. The spoken "I'm taking over now" heads-up is a
`prompt.py` rule for the confirmed call, not anything here. `look_at_screen`, `take_screenshot`,
`start_screen_recording` / `stop_screen_recording` and the sharing tools are the observation side;
screenshots and recordings reach the chat through the `ui_action` attachment shape (see
`jarvis/tools/CLAUDE.md`). `routes/control.py` serves status, stop, safety, and the screenshot and
recording files by opaque id.

## Gotchas

- **`SendKeys`-style typing goes to whichever window last had real OS focus**, so a click is never
  enough — always `focus()` first (see `desktop.py`).
- **Confirm the real OS side effect, not the loop's own claim of success.** A control session was only
  trusted after a real Notepad window titled exactly as instructed genuinely existed once it reported
  done.
