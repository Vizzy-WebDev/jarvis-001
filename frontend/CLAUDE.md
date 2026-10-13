# Front end (`frontend/`)

Next.js App Router + React + TypeScript + Tailwind, built to a **static export**
(`frontend/out`) that the Python backend serves itself — one process, one port, no Node
at runtime. `npm run build` produces the export; it is committed, so changing the front
end means rebuilding and committing `out/` alongside the source change.

**Four anchors are fixed; everything else is free to change.** The menu button is top
LEFT and reaches every page; the core stays centred on the stage with the mic beneath
it and nothing on the page may move or resize it; the conversation panel floats OVER the
right edge of the stage, reserving no column; and the conversation and composer are ONE
panel, not two. `tests/test_shell_e2e.py` asserts all four structurally — a redesign
that quietly breaks one fails there, where a screenshot review would not.

`frontend/lib/nav.ts`'s `PAGES` array remains the single source of truth for the
menu, the hash router (`lib/useHashRoute.ts`) and voice navigation (`open_section`,
whose names `tests/test_tools.py` checks against it). The menu is eleven rows (design
9a); five are grouped pages whose sub-pages are pill tabs hanging under the header
(design 10b): `#/knowledge/memory`. The ids pages had before grouping (`memory`,
`models`, `app-control`, `tasks`…) are `ALIASES` that still resolve — links, notices
and tests keep working without the address bar being rewritten. Adding a page or tab
is one entry there plus one case in `app/page.tsx`'s `screenFor()`. Only the first
segment (and a grouped page's tab) picks the screen; a screen may keep its own place in
the rest (`#/content/niche/Psychology`), so a refresh stays there and Back leaves it.

## The redesign (in progress, branch `claude/ui-redesign`)

The approved prototype and its decision log live outside the repo
(`Downloads/My Jarvis interface/design_handoff_jarvis_ui`); existing behaviour is the
functional source of truth, the prototype the visual one. Done so far: colours are CSS
variables (`app/globals.css`, mapped in `tailwind.config.ts`) so Appearance settings can
re-theme by rewriting variables; Geist/Geist Mono are self-hosted by the `geist`
package; the 10b page shell; the 9a menu with live badges; "Ask Jarvis" on every page;
Home (v6 + voice items 1c–1i, laptop / tablet / phone) in `components/home/`.

**Preview builds.** `JARVIS_BUILD_DIR=../.preview-build npm run build` builds somewhere
other than `out/`, and a server started with `JARVIS_FRONTEND_DIR` pointing there serves
it (the e2e suites honour it too). Unfinished work is checked that way, so the build a
running Jarvis serves from `out/` is only replaced when a stage is done.

**The conversation lives in `components/conversation/useAssistant.ts`**, held by the
root and never unmounted, so Home's panel and the "Ask Jarvis" dock on every other page
are the same conversation, not two transcripts kept in step. `app/page.tsx` is layout.

**Performance rules.** Each screen is its own chunk (`next/dynamic` in `app/page.tsx`),
prefetched once the app is idle. The screen element is memoised on page/tab, because
the conversation re-renders the root on every streamed word; a screen rebuilt with it
would make typing and scrolling stutter. Screens are not kept alive between visits on
purpose: they read fresh data on open, and a kept-alive screen would show stale lists.

## Screens (`components/screens/`)

One component per page or tab, rendered into `components/shell/PageShell.tsx`, which
draws the header: the menu button, a mark of the core in Jarvis's current colour, the
page's name as the screen's one `<h1>`, the pill tabs and the live state label. A
screen not yet redesigned gets its old one-line blurb above it (`Blurb`).

**A screen must NOT render its own `<h1>`.** The shell already drew it; there is a
test that walks every page and asserts exactly one.

**The standing rule: if a thing is a thing, it is clickable.** Every list of real
objects gets a real detail view and real actions. No screen ships as a read-only display
of rows.

## Shared primitives (`components/ui/`)

Every screen composes from these, and the sameness is most of what makes twelve screens
feel like one product: `Card`/`Row`, `Button` (three tones, never more), `Field` +
`inputClass`, `Toggle`, `Modal`, `Popover`, `EmptyState`, `AppIcon`,
`IconButton`, `Icons`. No UI libraries beyond Tailwind.

**`Modal` nests correctly and `Popover` escapes a scrolling modal body** — both were
real bugs. Every open overlay pushes onto a module-level stack (`ui/overlay-stack.ts`)
and only the topmost responds to Escape, so a list opened from inside an editor does not
take the editor down with it. The effect that registers this is keyed on `open` ALONE,
with the close callback read from a ref: including `onClose` in its dependencies makes it
re-run on every render (callers pass inline arrows), which silently re-promotes the
overlay to the top of the stack and reintroduces the bug. `Popover` positions itself
`fixed` from the trigger's measured rect rather than absolutely, because a modal body
scrolls and would otherwise clip it. **It is rendered into `document.body` through a
portal, and that is not optional**: `position: fixed` means "relative to the viewport" only
when no ancestor has a `transform`, `filter` or `backdrop-filter`, and the conversation
panel has the last (its rail the first). Declared inside it, the composer's model picker was
placed relative to the panel — 1075px became 2116px on a 1440px screen — and clipped by its
overflow: open, but where nobody could see it. Tests passed anyway, because Playwright will
scroll a hidden container into view and click; `assert_really_visible()` in
`test_shell_e2e.py` asks what a person's eyes would (inside the viewport, and what sits at
its own centre). It also watches its own size with a `ResizeObserver`, because it is placed
from its height and a picker's height changes while open (a section appears when a model is
chosen); worked out only once, it ran off the bottom of the screen until something else
moved it.

**`Field` is deliberately a `div`, not a `label`.** A label forwards a click anywhere
inside it to the first labelable control — found the hard way when a field containing an
"Add connector" button and a popover full of switches reopened the popover on every
attempt to close it.

**`Toggle` declares `data-testid` as a real prop.** TypeScript does not check hyphenated
JSX attributes against a component's props, so passing one to a component that does not
accept it typechecks cleanly and is then silently dropped.

## The typed client (`lib/api.ts`)

Every route goes here and nowhere else, with its types in `lib/api-types.ts`. Same-origin
by design: in production the backend serves these files and the API from one port, and in
development `next.config.mjs` rewrites `/api` to the running backend, so nothing needs a
base URL and there is no CORS. `ApiRequestError` carries the server's own message —
backend error strings are written in plain language for the user to read directly, so
they are surfaced as-is rather than replaced with a generic failure.

## Voice engine (`frontend/lib/voice/`)

Three engines, all extending the shared `VoiceEngine` contract
(`lib/voice/engine.ts` — `.on(event, handler)`/`.emit()`, `start`/`stop`/`sendText`/
`interrupt`/`setMuted`), so `app/page.tsx` wires UI to whichever is selected in
settings and doesn't otherwise care which one it's talking to. Events: `state`,
`transcript`, `chunk`, `tool`, `tool_result`, `model_switch`, `style_floors`,
`reaction`, `restart`, `stt_fallback`, `paused`, `tts_failure`, `done`, `saved`, `error`.

**`saved` carries the server's stored ids for a spoken turn** (`routed`'s `userMessageId`,
`done`'s `messageId`), and `app/page.tsx` swaps them for the placeholder ids (`t7`) — the
same swap `send()` does for a typed message. Without it, Edit/Retry on something SAID sent
the server an id it had never heard of: it cut nothing, the replaced exchange stayed in
Jarvis's memory, and it reappeared on reload. `tests/test_voice_edit_e2e.py` drives a real
engine with a stand-in recogniser and a silent fake microphone and checks the stored
messages, not the screen.

- **`PipelineEngine`** (`pipeline-engine.ts`) — Engine A. Chrome's own
  `SpeechRecognition` → any model, over the ordinary `/api/chat/stream` path →
  server-side or browser TTS. Three separately-swappable hops, not a fused
  pipeline.
- **`DuplexEngine`** (`duplex-engine.ts`) — Engine C, "keeps listening while it
  talks." Recognition goes over `/api/duplex` (a provider when a key is
  configured, the browser's own otherwise), reasoning is the ordinary chat
  stream, speech is any configured voice — four layers, kept genuinely
  separate. Self-echo is prevented by not SENDING mic frames while it speaks
  (plus a short tail), never by filtering audio after the fact — three
  filtering approaches were tried and all three still let some of the
  assistant's own voice through; barge-in still works because interruption is
  detected from local mic energy, unaffected by whether frames are being sent.
- **`RealtimeEngine`** (`realtime-engine.ts`) — Engine B, a provider's own
  speech-to-speech session relayed by the server over `/api/live`. The route
  has no session implementation at the moment (it answers "No model with a
  realtime voice is set up yet."), so the picker never offers this engine.
  **Nothing here names a provider.** Mic audio is NOT held back while it speaks,
  unlike the other two — its interruption detection is server-side and
  depends on hearing the person while its own audio plays, so gating the
  upload would silently disable that entirely.

**All three voice-output paths expose a real `getOutputLevel()` (0..1) for the core's
audio-reactivity (`lib/core.ts`, fed by `components/home/HomeScreen.tsx`)** — none of them are a hardcoded 0:
- `RealtimeEngine` computes RMS inline from each scheduled PCM chunk as it plays.
- `frontend/lib/voice/audio-player.ts` (server-side TTS — `speech/tts/matching.py`'s provider
  registry, e.g. ElevenLabs; the free `browser` voice is `PipelineEngine`'s
  actual default, not this) reads an
  **offline-decoded amplitude envelope** (`frontend/lib/voice/voice-envelope.ts`'s
  `buildEnvelope()`/`sampleEnvelope()`) built from a SEPARATE copy of the same
  audio bytes via `OfflineAudioContext`, sampled against the real `<audio>`
  element's own `currentTime`. **This deliberately never touches the real
  playback graph.** An earlier version tapped a live `AnalyserNode` directly
  onto the TTS `<audio>` element for the same purpose, and it was reverted —
  once an element is routed through `createMediaElementSource`, it plays ONLY
  via that graph, and if the shared `AudioContext` was ever suspended
  (autoplay-policy territory) when a reply started, `resume()` is async and
  isn't something `play()` waits on, risking a clipped or silent first moment
  of speech. `new Audio(url); audio.play()` stays exactly that plain; the
  offline-decode approach is what actually shipped instead. **Don't reintroduce
  a tap on the live playback path — extend the offline-envelope approach
  instead if the orb's own-voice reactivity ever needs more fidelity.**
- `frontend/lib/voice/browser-speaker.ts` (the `browser` `speechSynthesis` voice-output setting)
  has no analysable audio to read at all, so its `getOutputLevel()` is
  timing-derived instead: each `onboundary` word event re-triggers a short
  decaying pulse (~220ms), giving the orb a real per-word rhythm rather than
  flat procedural motion.

`getMicLevel()` is real on both engines: `PipelineEngine` reads
`MicLevelMonitor`; `RealtimeEngine` computes RMS inline from each
`onaudioprocess` frame.

**The mic never re-enters while Jarvis is talking, on purpose.**
`pipeline-engine.ts`'s continuous `SpeechRecognition` opens its own separate,
unprocessed mic capture — `echoCancellation: true` on `getUserMedia()` never
reaches it — so without suspension Jarvis hears its own voice as user input.
Recognition is suspended for the window Jarvis's audio is *actually playing*
plus a ~700ms tail (Chrome's cloud ASR runs 300-800ms behind real time).
**The suspend call must live in `_onSpeechStart()`, not `_send()`** —
suspending from `_send()` fires at "thinking", before any audio exists to
echo, and kills the mic for the whole thinking phase too. If self-listening
ever resurfaces, check first whether `_recSuspended`/`_isSpeaking` cover the
*speaking* window specifically, not thinking. Barge-in (talking over Jarvis)
samples mic energy on a fixed 100ms timer, not `_onResult` (unreliable — can
fire on two loud instants seconds apart). `MicLevelMonitor._speakingSince`
(`frontend/lib/voice/turn-detector.ts`) is reset via `reset()` at the top of every
`_startBargeInSampler()` run — it must never carry a stale timestamp across
turns, or the barge-in sustain gate can trigger almost instantly on the
*next* reply.

**Don't gate `RealtimeEngine`'s mic upload for echo reasons the way
`PipelineEngine` is gated above.** The realtime provider's own barge-in depends on
its server-side voice detection hearing the user while it's talking; gating
`onaudioprocess` during Jarvis's own playback would silently disable that
entirely, since it can't detect being talked over in audio it was never
sent. `RealtimeEngine` streams mic audio continuously and lets the provider's own
`interrupted` event handle it — one duplex socket, not two separate
capture/playback pipelines like the Pipeline engine.

**"Jarvis's voice just stops" has three unrelated causes** — check which one
actually matches: (1) a failed `/api/tts` fetch resolving silently to `null`
with no retry — now retries once. (2) A mid-stream chat-stream error not
calling `speaker.end()` — now every error path calls it. (3) Chrome's
`speechSynthesis` silently dropping an utterance with neither `onend` nor
`onerror` firing — now has a per-utterance watchdog that force-continues if
Chrome never confirms.

## Home (`components/home/`) and the core (`lib/core.ts`)

`HomeScreen.tsx` lays Home out (design Home v6) and owns what only Home needs: the
breakpoints (phone < 700px, tablet upright, laptop < 1400px, desk), presence
(`presence.tsx`: resting after `restAfterMin` idle minutes, the ~2.8s wake sequence and
its boot lines, the Night-stand clock), the wake-word listener, push-to-talk and Home's
keys (Esc stops Jarvis speaking or ends a session; F = Just Jarvis; Space when
push-to-talk is on). Home's settings — what is shown, rest/activation, wake phrases,
push-to-talk, speak replies, the engine and voice ids — live in `/api/prefs` through
`lib/usePrefs.ts` (one module store, optimistic writes); they used to reset on reload.

**The core is a 2D canvas** (`lib/core.ts`, ported from the approved design: rays,
drifting dots, rings, ambient light and horizon). It replaced a three.js shader sphere —
no WebGL context, no 600 KB library, no fallback path. One animation loop draws the core
AND the two level meters (under the mic, in the header's state pill), capped at ~30fps
and idle while the canvas is off screen or the tab is hidden. It reads everything per
frame through an `input()` function backed by refs, so it never re-renders React; a
real engine level (mic while listening, output while speaking) replaces the designed
motion when there is one. The five states and their colours are in `lib/jarvis-state.ts`
(fixed, never themeable; only Standby deepens in light mode).

**The mic (`[data-testid=mic]`, design 1c)** starts a voice session when none is running
— the same as saying the wake word — and mutes/unmutes one that is. It never ends or
interrupts anything: **Interrupt** is its own red ring to the LEFT of the mic, there only
while Jarvis is speaking (and Esc), and Esc while listening ends the session. The phone
layout keeps a separate mute button, as designed. `toggleMute` in `useAssistant.ts`
touches only microphone capture — never `interrupt()` or `stop()`.

**The wake word** (`lib/voice/wake-listener.ts`) streams 16 kHz PCM to this app's own
`/api/voice/wake`, scored by the on-device model; the audio never goes further. It runs
while no voice session is (one recogniser at a time) and while "Hey Jarvis" is in the
phrase list — the model hears that phrase only; others are kept and marked "not heard
yet". The server fetches the model once into `data/wakeword/` behind the
`JARVIS_WAKE_MODEL_DOWNLOAD` interlock and answers `preparing` meanwhile; the listener
retries.

**Editing (design 1h)** loads a sent message — text and files — into the composer,
tagged "Editing message"; Resend replaces the replies after it (the same
truncate-and-resend as before), Cancel or Escape leaves it.

**`setMuted()` never clears `pendingUtterance`/`silenceTimer`/
`pendingConfidence`, and `_maybeFinalize()` doesn't check `this.muted`
either.** Speech isn't sent the instant recognition produces a final result —
`_onResult()` starts a `silenceTimer` (`computeWaitMs()`, ~0.7-2.5s) to
confirm the user is actually done before calling `_maybeFinalize()`/
`_send()`. Muting only ever blocks **future** capture, never delivery of
something already said — safe specifically because `_onResult()`'s own
`this.muted` guard already prevents any *new* speech from entering
`pendingUtterance` while muted, so the only content `_maybeFinalize()` can
ever see is something captured before muting took effect. **General lesson:
when a fix clears/blocks something "just to be safe," check whether the
thing being cleared could legitimately have been produced BEFORE the
triggering condition, not just during or after it** — `pendingUtterance` at
mute-time is always pre-mute content.

## Delivering a real image/video into the transcript

A tool result can put a real, visible image or video into the current reply, not just
describe it in words — see `abilities/tools/CLAUDE.md`'s own entry on the attachment
convention for the server side (`take_screenshot.py`/`tools/screen_recording.py`).
This is ordinary React, not DOM manipulation: `Message.tsx`'s `attachmentOf(event)`
reads `event.attachment` off a `tool_result` turn event
(`{kind, url, mimeType}`), and `app/page.tsx`'s handler does
`patch((turn) => ({ ...turn, attachment }))` on that turn's state — no imperative
node creation, no separate "does the bubble exist yet" check, since React re-renders
the turn from its own state regardless of when the attachment arrives relative to the
reply text. `Message.tsx` then renders `turn.attachment?.kind === 'image'`/`'video'`
conditionally in JSX (a plain `<img>`/`<video controls>`), alongside `turn.text`.

Because attachment and text both live as plain fields on one immutable `Turn` object
rather than as mutated DOM nodes, two classes of bug are structurally impossible here: appending more reply text can never wipe an
already-set attachment (React reconciles from state, it doesn't mutate a text node in
place), and there is no bespoke "is this bubble empty" check to keep in sync with what
counts as content — a turn with an attachment and a turn with text are just two fields
on the same object, checked directly rather than inferred from a DOM query.

## Artifacts (`components/artifacts/`, `components/screens/ArtifactsScreen.tsx`)

One viewer, `ArtifactViewer`, for every kind of file Jarvis makes — used by the chat's file
card (`ArtifactCard` → `ArtifactPanel`, a Modal portalled into `document.body` for the same
`backdrop-filter` reason as `Popover`) and by the Artifacts page. **It never asks the server to
serve anything inline**: it fetches the bytes and renders them itself — text and code as text
nodes, Markdown through `markdown.tsx` (an in-repo renderer that never produces HTML; the parser
is `lib/markdown.ts`), SVG and images through `<img>`, PDF in the browser's viewer from a blob,
Word/Excel/PowerPoint from the JSON `/preview` route, and a web page in
`<iframe sandbox="allow-scripts">` with `lib/artifacts.ts::lockedDocument()` putting a strict
content policy first in its document. Never add `allow-same-origin` to that frame; the backend's
`request_guard.py` is the other half of keeping a model-written page away from Jarvis.

A reopened chat rebuilds its cards from the saved tool results (`app/page.tsx::turnsFrom`,
`lib/artifacts.ts::cardsFromToolResult` — the same reading the server does live). Open in Chat
on the Artifacts page reopens the conversation that MADE the file and highlights its card
(`[data-artifact-id]`, `data-focused`). The pure pieces are unit-tested in
`test/artifacts.test.mjs`.
