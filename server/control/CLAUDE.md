# Computer control (`server/control/*.js`)

Lets Jarvis actually operate the desktop — click, type, read windows, launch apps —
toward a stated goal. Deliberately **not** built on `models/runner.js`'s chat loop: a
control session is long-running, carries screenshots, and must never be able to
reschedule itself mid-click via a tool like `schedule_task`. It has its own loop and its
own small fixed tool set.

**A real control session has now been watched completing a task end-to-end
(`launch_app` → `type` → verify → `report_done`), independently confirmed against
the real OS afterward, not just trusting the loop's own claim** — a real Notepad
window titled exactly as instructed genuinely existed once the session reported
done. This closes what had been an open item since the loop was first built (no
session had ever been observed reaching `report_done` unbroken). Finding it required
fixing a real, confirmed, pre-existing bug along the way — see the Gotchas section's
"connector tool declarations leaked internal fields into the model" entry — the loop
had likely never worked with Gemini and any connector enabled (effectively always,
since `browser`/`files` auto-register at startup) until that fix.

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
  `tools/index.js` or `capabilities.js` (see the circular-import invariant in the root
  `CLAUDE.md`); a
  control session's tool list is its own `CONTROL_TOOLS` plus merged connector tools,
  not the chat skill catalog. Tracks `preExistingHandles` vs. `createdHandles` per
  session and only ever auto-closes windows Jarvis itself opened for the current task at
  `report_done` — closing anything pre-existing or unsaved-looking always confirms first
  (a risky action). **PERCEIVE excludes Jarvis's own browser tab from the front-window
  pick** (reuses `tools/look_at_screen.js`'s `isJarvisOwnWindow()`, same reasoning as
  that file's own gotcha below — a control session used to have no equivalent guard,
  so it could start by reading/acting on Jarvis's own tab instead of the app the user
  meant). **DECIDE now falls back across up to 3 ranked candidate models**
  (`pickModelCandidates()`), same shape as `models/runner.js`'s own chat-turn fallback —
  confirmed gap: a single rate-limited/unreachable model used to fail the whole session
  on step one, with no fallback at all; only triggers on a genuine adapter throw, never
  on a timeout (a slow-but-working model still gets its full `DECIDE_TIMEOUT_MS`).
  **A risky-action confirmation now times out** (`CONFIRM_TIMEOUT_MS`, 10 minutes) rather
  than waiting forever — a missed SSE event used to hang the whole session with nothing
  visibly failing; a timeout resolves as a decline, the same outcome as a real "no".
  **A `launch_app` action polls for the new window to actually appear** (up to 5s, every
  300ms) instead of one fixed 900ms sleep — a cold-starting app (Windows 11's own Store
  Notepad, confirmed slow) could still be missing from the very next PERCEIVE otherwise,
  which could read to the model as "the launch failed" and prompt a second copy.
  **A batched `perform_actions` call reports every action, even ones a window-changing
  action earlier in the same batch left unattempted** — those used to get no result
  entry at all, which could read as "it also succeeded" rather than "never ran" (the
  reported case: a batched `[launch_app, type]` silently dropped the `type`).
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
- **`observation-bridge.js`** — the always-on-top blue "Jarvis can see your screen"
  badge, reference-counted across independent callers (`look_at_screen.js`'s one-off
  glance, a `screen_looks_like` monitor's ongoing watch, and now `screen-share-state.js`'s
  persistent Screen Sharing mode below) — see its own header comment for the token
  design. A spoken heads-up ("I'm taking over now, hands off the keyboard and mouse")
  is a `prompt.js`-level instruction (the SECOND, confirmed `control_computer` call
  specifically), not anything in this directory — it's ordinary reply text the model
  generates for that turn, spoken exactly like any other reply.
- **`screen-share-state.js`** — Screen Sharing as a persistent ON/OFF mode, deliberately
  separate from `observation-bridge.js`'s own per-call token accounting: `look_at_screen`/a
  monitor light the badge for exactly as long as one capture is happening; Screen Sharing
  is a real mode the user (or a spoken "share my screen with me") turns on and leaves on.
  Turning it on never itself triggers a description — nothing was asked yet — it only
  arms the badge and tells the model, via a `prompt.js` volatile section gated on
  `isSharing()`, that the next screen-related question doesn't need "look at my screen"
  said first. `server.js`'s `/api/observation/share/start`/`/stop` back a real header
  toggle (`public/app.js`'s `setupScreenShareToggle()`) AND the voice-facing
  `share_screen.js`/`stop_sharing_screen.js` tools — either path updates the same state,
  so the toggle always reflects a spoken instruction and vice versa, per the requirement
  that the two work alongside each other. The existing dot-click `/api/observation/stop`
  also stops sharing now, as one unified "stop whatever's making this badge lit" action.
- **`screen-recorder.js`** / **`recording-store.js`** — real video screen recording via
  ffmpeg (`gdigrab` desktop capture → `libx264`/`yuv420p`/`+faststart` for a file any
  `<video>` element can actually play), resolved the same way `browser.js` resolves a
  real Chrome (a couple of common install paths, then whatever the OS's own PATH
  resolves) — never bundled, never a hard dependency; a plain-language explanation of
  what ffmpeg is and how to add it is returned instead of a raw error when none is found.
  Stopped gracefully via ffmpeg's own `q`-on-stdin convention (never killed outright,
  which can leave a corrupt file) and force-killed only as a bounded last resort.
  Genuinely independent of the perceive/act loop above — a recording keeps running
  through an active control session. `recording-store.js` mirrors `screenshot-store.js`'s
  save/list/prune-by-count-and-age shape, just for `.mp4`s, at a much lower default
  retention count (`safety.js`'s `recordingRetention`) given the file size. Delivered into
  the chat as a real, playable attachment — see `take_screenshot.js`'s own entry in
  `server/tools/CLAUDE.md` for the shared `ui_action:{type:'attachment'}` delivery
  mechanism both this and a screenshot use. **Verified live**: a real ~3-second recording
  produced a genuinely valid, cleanly-decoding `.mp4` (confirmed with `ffmpeg -f null -`
  reporting zero errors), not just a non-empty file.

## Gotchas

- **Connector tool declarations merged into the control loop's own `tools` array carried
  internal bookkeeping fields the model was never meant to see — Gemini's strict schema
  validation rejected the whole request outright, every time.** `connectors/index.js`'s
  `getToolDeclarations()` attaches `connectorId` (which connector a tool belongs to) and
  `confirm` (risk level) directly onto each declaration — fields `capabilities.js`'s OWN
  `getToolDeclarations()` strips before the normal chat path ever sees them (its own doc
  comment: "model-facing, stripped to `{name, description, parameters}`"). `session.js`
  called `connectors/index.js` directly, bypassing that stripping entirely. Confirmed
  live: a real control-loop DECIDE call against Gemini failed with a 400 — "Unknown name
  `\"connectorId\"` at `'tools[0].function_declarations[3]'`: Cannot find field" — the
  instant ANY connector was enabled, which is effectively always (the `browser`/`files`
  singletons auto-register at server startup). This most likely explains why no control
  session had ever been observed reaching `report_done` before this fix — the loop's own
  fallback across ranked candidate models doesn't help when the failure is a malformed
  REQUEST, not a model being unavailable, and Gemini is a common default candidate.
  Fixed by building a second, model-facing array
  (`{name, description, parameters}` only) for what's actually sent to `adapter.stream()`,
  while keeping the full, unstripped `connectorDeclarations` for this loop's own dispatch
  (`.find()`, `.confirm`, `runConnectorTool()`) — the same two-shapes-for-two-audiences
  split `capabilities.js` already draws for the ordinary chat path. Re-verified live after
  the fix: the identical goal (open Notepad, type text) completed with `status:'done'`,
  independently confirmed against the real, running Notepad window afterward — not
  trusting the loop's own claim of success (per root `CLAUDE.md`'s testing discipline).
  Anthropic/OpenAI-compatible tolerate unrecognized schema fields more permissively, which
  is very likely why this went uncaught for as long as it did — whichever model happened
  to be live when the loop was previously exercised was probably never Gemini specifically
  with a connector enabled.
- **A `type` action's own `RISKY_KEYWORDS` scan was matching ordinary typed
  sentences, not just dangerous ones.** `label` for a `type` action is the LITERAL text
  being typed — and that same everyday-language list (tuned for a tool/action's own
  NAME or description, where "update"/"write"/"move"/"buy"/"post"/"message" are
  legitimate signals) matches constantly in ordinary prose ("write a note", "Buy milk,
  eggs and bread", "Remember to post the invoice" all confirmed live to trip 'risky').
  The original motivating case for scanning typed text at all (see
  `classifyActionRisk()`'s own doc comment) was catching something like `rm -rf` typed
  into a terminal — a destructive COMMAND, not ordinary language. Fixed with a second,
  narrow, substring-matched `DANGEROUS_TEXT_PATTERNS` list (`rm -rf`, `format c:`,
  `drop table`, a shell fork bomb, ...) used ONLY for a `type` action's `label`;
  `description` (the model's own stated reasoning, once actually threaded through —
  see the next gotcha) and every non-`type` action still go through the original
  broader `RISKY_KEYWORDS` word-tokenized scan unchanged.
- **`action.reasoning` was always `undefined`, silently dropping the model's stated
  intent from ever reaching the risk check.** `reasoning` is a field on the
  `perform_actions` call itself (a sibling of `actions`), not per-action — `session.js`
  was reading `action.reasoning` inside the per-action loop, which no schema anywhere
  actually populates. Fixed by threading the batch's one `reasoning` string into
  `actOnBatch()` as its own parameter, applied to every action in that batch (the best
  available signal, since the schema has no true per-action equivalent).
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
- **`capabilities.js`'s confirm-token redemption (formerly in the merged
  `skills/index.js`) used to require the model's "yes, do
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
