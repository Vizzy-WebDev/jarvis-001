# Computer control (`server/control/*.js`)

Lets Jarvis actually operate the desktop — click, type, read windows, launch apps —
toward a stated goal. Deliberately **not** built on `models/runner.js`'s chat loop: a
control session is long-running, carries screenshots, and must never be able to
reschedule itself mid-click via a tool like `schedule_task`. It has its own loop and its
own small fixed tool set.

- **`agent.ps1`** — the actual mouse/keyboard/screen primitives, as one long-lived
  PowerShell process (`ps-bridge.js` owns it) reading one JSON command per line on
  stdin, writing one JSON result per line on stdout. Commands: `windows`, `focus`,
  `switch_window` (backed by `focus`), `minimize_window`, `restore_window`
  (`SW_RESTORE`), `arrange_window` (`SetWindowPos`), `close_window` (`PostMessage` +
  `WM_CLOSE` — never `Stop-Process`, see the shared-host-process gotcha below),
  `read_window` (UI Automation — exact button names/values/positions, the
  accurate-aim path), `screenshot` (fallback path — also the path
  `skills/look_at_screen.js` uses for observation-only, no-control requests; that
  skill's `pickWindow()` excludes Jarvis's own window from the candidate pool first —
  see the gotcha below), `click`/`double_click`/`right_click` (each
  now glides the cursor there over ~250ms via `Move-CursorSmoothly` — an eased series of
  position updates, not an instant jump — so the user can actually see where Jarvis is
  about to click; ends exactly on target, so `session.js`'s post-click cursor-drift
  stop-guard is unaffected), a standalone `move_cursor`, `type`, `key`, `scroll`,
  `cursor`, `idle`, `processes`.
- **`ps-bridge.js`** — one persistent `powershell.exe -STA` process (see the STA gotcha
  below), request/response matched by numeric id, auto-restarts if the process dies.
- **`session.js`** — the loop: PLAN (one model call, shown to the user for approval) →
  repeat PERCEIVE (free — window list + front window's UI tree or a vision-gated
  screenshot fallback) → DECIDE (one model call against a small fixed `CONTROL_TOOLS`
  set — `launch_app`/`switch_window`/`minimize_window`/`restore_window`/
  `arrange_window`/`close_window`/click/type/key/scroll/`wait`/`report_done`/
  `report_stuck`, plus whatever `connectors/index.js` currently has enabled merged in, so
  a desktop task can call a connected service mid-run through the same confirm gate) →
  GUARD (`guard.js`, evaluated against whichever window the action actually targets) →
  ACT (`ps-bridge.js`) → back to PERCEIVE, which doubles as verification (the model sees
  the result of its last action in the next PERCEIVE and decides whether to
  proceed/retry/finish) — until done/stuck/stopped/a step cap. Never imports
  `skills/index.js` (see the circular-import invariant in the root `CLAUDE.md`); a
  control session's tool list is its own `CONTROL_TOOLS` plus merged connector tools,
  not the chat skill catalog. Tracks `preExistingHandles` vs. `createdHandles` per
  session and only ever auto-closes windows Jarvis itself opened for the current task at
  `report_done` — closing anything pre-existing or unsaved-looking always confirms first
  (a risky action).
- **`guard.js`** — `classifyActionRisk()` (safe/notable/risky — keyword-scanned BEFORE
  the kind lookup, so a low-level primitive like `key` or `type` still escalates if what
  it's actually doing sounds irreversible) and `checkBlocklist()` (window title/process/
  URL against `safety.js`'s configured patterns). A risky action pauses mid-loop via a
  real `Promise` the session awaits, resolved by `POST /api/control/confirm` — not a
  polling loop.
- **`overlay.ps1`** / **`overlay-bridge.js`** — the always-on-top red control bar,
  spawned as its own process so it stays reachable even while a control session has
  focused a different window. Non-activating (`WS_EX_NOACTIVATE`) so it never steals
  keyboard focus.
- **Three independent stops**: the overlay button, the global hotkey (`Ctrl+Alt+J`,
  polled via `GetAsyncKeyState`, not `RegisterHotKey`), and moving your own mouse
  (cursor position is compared before every synthetic mouse action; a real move stops
  the session, it does not just pause it).

## Gotchas

- **`classifyActionRisk()` lowercased an identifier before ever checking its camelCase
  boundaries — again — undoing the exact fix this file's own history already made
  once.** The genuinely wrong-but-not-obviously-wrong version: `const kind =
  String(action?.kind || '').toLowerCase()` ran BEFORE `kind` was folded into the text
  handed to `words()`, so by the time `words()`'s own (correctly-written)
  `.replace(/([a-z])([A-Z])/g, ...)` ran, there was no uppercase letter left anywhere to
  find a boundary at — "updatePet" had already become "updatepet" one line earlier, and
  a real MCP tool named like `COMPOSIO_MULTI_EXECUTE_TOOL` never tokenized into
  `["...", "execute", ...]` at all. Confirmed live: every actual Composio tool call
  (the platform's own generic multi-purpose dispatcher, `COMPOSIO_MULTI_EXECUTE_TOOL`)
  was always classified 'risky' — not because of anything it was actually doing, but
  because "execute" is baked into its own name as a structural word-part, and the
  keyword scan was accidentally seeing it via `RISK_BY_KIND`'s fallback path regardless.
  User-visible symptom: every single action through a connected Composio/Smithery/
  Zapier-style gateway needed confirmation, felt like "everything requires permission."
  Fixed by keeping the identifier's real casing all the way into `words()` — but ONLY
  for the identifier (`kind`); free-flowing prose (`label`/`description`) is
  deliberately lowercased *before* tokenizing instead, since running the same
  camelCase-split regex over natural language finds accidental boundaries inside
  ordinary capitalized proper nouns ("SharePoint" -> "Share"+"Point" -> matches the
  keyword "share") — the exact false-positive class this file was already rewritten
  once to avoid. Two different text shapes, two different rules, both correct now;
  neither one alone is.
- **A generic multi-purpose "run whatever tool was found" dispatcher (Composio's
  `COMPOSIO_MULTI_EXECUTE_TOOL`, and any future gateway-style MCP server shaped the same
  way) is a real, structural blind spot for a static, per-declaration risk classifier.**
  Fixing the camelCase bug above stops the false 'risky' that came from the tool's own
  name, but the classifier still has no visibility into WHAT a given call is actually
  asking the dispatcher to do — its real danger lives in its *arguments* (a
  `tool_slug`/`toolkits[].name`-style field naming the real underlying action, e.g.
  "GMAIL_FETCH_EMAILS" vs "GMAIL_SEND_EMAIL"), which `classifyToolRisk()` never sees —
  risk is computed once, statically, when the tool is first declared to the model, not
  per actual call. `COMPOSIO_MULTI_EXECUTE_TOOL` is therefore now uniformly 'notable' —
  correctly no longer blocking a harmless read behind confirmation, but also not
  catching a genuinely dangerous write/delete/send performed through it either. Real
  per-call, args-aware risk classification (reading the dispatcher's own arguments, not
  just its static declaration) is the honest fix if that gap ever needs closing —
  bigger change, deliberately not attempted here.
- **`skills/index.js`'s confirm-token redemption used to require the model's "yes, do
  it" call to resend byte-identical arguments to the original ask** (`JSON.stringify`'d
  and compared for exact equality). Fine for a skill with one or two simple string
  arguments; a real bug for anything with a complex/nested shape, since a model
  regenerating "the same" call has no guarantee of reproducing an identical object.
  User-visible symptom: the user says yes, the equality check silently fails, a FRESH
  token gets minted, and the same question comes back — looking exactly like an
  infinite confirmation loop that never actually completes no matter how many times
  the user confirms. Fixed by trusting the token alone as proof of consent (already
  random, single-use, skill-name-scoped, 5-minute TTL) and running with the ORIGINAL
  args captured when the token was issued, never whatever the model resent alongside
  it — removes the fragile equality check entirely rather than trying to make it more
  lenient.
- **A synthetic mouse click does not reliably restore keyboard focus** — `SendKeys`
  targets whichever window last had real OS foreground focus, and Windows' anti-focus-
  stealing rules can suppress a synthetic click's usual side effect of shifting that
  focus. `type`/`key` can silently go to the wrong window without an explicit `focus`
  command immediately before them — `session.js` always calls `focus` before acting on
  a window, never relies on a click alone.
- **PowerShell's inline `Add-Type -TypeDefinition` does NOT automatically see
  assemblies loaded via a separate `Add-Type -AssemblyName` call** — a C# class
  inheriting from `Form` (or using anything from `System.Windows.Forms`) needs
  `-ReferencedAssemblies 'System.Windows.Forms','System.Drawing'` on that specific
  `Add-Type` call, or the compile fails with "type or namespace 'Forms' does not exist"
  even though the assembly is already loaded and usable from plain PowerShell code in
  the same script (`overlay.ps1`'s `NoActivateForm` class).
- **UI Automation and the clipboard both expect a single-threaded apartment (STA)** —
  PowerShell's default is MTA. `ps-bridge.js` launches `agent.ps1` with `-STA`
  specifically for `read_window` (`AutomationElement`) and the clipboard-paste path in
  `type`; `overlay.ps1` has run fine without it so far but add `-STA` there too if it
  ever shows the same class of intermittent failure.
- **A P/Invoke signature you author yourself doesn't have to match the Win32 header's
  types** — `mouse_event`'s `dwData` is declared `uint` in the Windows API, but a
  negative wheel-scroll delta then fails a checked cast in PowerShell
  (`Cannot convert value "-360" to type "System.UInt32"`). Since this is our own
  `Add-Type` signature, not a fixed external contract, declaring it `int` instead fixes
  it — the native function only cares about the bit pattern, not the CLR type
  describing it.
- **Windows 11's built-in Notepad can share ONE process across multiple open windows**
  — `Stop-Process` on one Notepad window can close a completely different, unrelated
  Notepad window sharing the same PID. **Never `Stop-Process` a shared-host app like
  Notepad to clean up a test window** — use its own UI (focus the window, `Ctrl+W`), or
  `agent.ps1`'s real `close_window` (`WM_CLOSE` via `PostMessage`, never `Stop-Process`).
- **`skills/look_at_screen.js` must exclude Jarvis's own window before matching** — the
  OS foreground window while someone is *typing to Jarvis* is Jarvis's own browser tab,
  so a naive front-window fallback (or even explicit targeting like "my Chrome window")
  could capture Jarvis itself instead of the app the user meant. `pickWindow()` excludes
  any window matching `isJarvisOwnWindow()` (known browser process + title exactly
  "Jarvis" or starting with "Jarvis -") from the candidate pool, falling back to
  including it only if it's genuinely the only window open.
- **A one-off Node test script that imports `ps-bridge.js` and then calls
  `process.exit(0)` can crash on its way out** with a libuv assertion
  (`!(handle->flags & UV_HANDLE_CLOSING)`, exit code 127) — this is libuv objecting to
  the forced exit while the persistent `agent.ps1` child process handle is still open,
  not a bug in whatever was being tested. The real server never force-exits mid-session,
  so this never happens in production; confirm the actual side effect (a written file, a
  persisted store entry) completed before treating the crash-on-exit noise as real.
