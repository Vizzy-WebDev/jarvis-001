# Front end (`frontend/`)

Next.js App Router + React + TypeScript + Tailwind, built to a **static export**
(`frontend/out`) that the Python backend serves itself — one process, one port, no Node
at runtime. `npm run build` produces the export; it is committed, so changing the front
end means rebuilding and committing `out/` alongside the source change.

**Four anchors are fixed; everything else is free to change.** The hamburger is top
LEFT and reaches every section; the orb stays centred on the stage with the mic beneath
it and nothing on the page may move or resize it; the conversation panel floats OVER the
right edge of the stage, reserving no column; and the conversation and composer are ONE
panel, not two. `tests/test_shell_e2e.py` asserts all four structurally — a redesign
that quietly breaks one fails there, where a screenshot review would not.

`frontend/lib/nav.ts`'s `SECTIONS` array remains the single source of truth for the
drawer, the hash router (`lib/useHashRoute.ts`) and voice navigation
(`open_section`). Adding a section is one entry here plus one screen component plus one
line in `app/page.tsx`'s `screenFor()`. Only the hash's FIRST segment picks the section; a
screen may keep its own place in the rest (`#/content/niche/Psychology`), so a refresh stays
there and Back leaves it.

## Screens (`components/screens/`)

One component per section, rendered into `GenericScreen`, which draws the shell chrome:
its own hamburger, the section's group label, and the `PageHeader` carrying the title
and blurb.

**A screen must NOT render its own `PageHeader`.** `GenericScreen` already drew it, so
doing both puts the title on screen twice and double-pads the column — a real bug that
shipped across seven screens and was caught only by a Playwright strict-mode violation,
by accident. There is now a test that walks every section and asserts exactly one `<h1>`.

**The standing rule: if a thing is a thing, it is clickable.** Every list of real
objects gets a real detail view and real actions. No screen ships as a read-only display
of rows.

## Shared primitives (`components/ui/`)

Every screen composes from these, and the sameness is most of what makes twelve screens
feel like one product: `Card`/`Row`, `Button` (three tones, never more), `Field` +
`inputClass`, `Toggle`, `Modal`, `Popover`, `EmptyState`, `PageHeader`, `AppIcon`,
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

**All three voice-output paths expose a real `getOutputLevel()` (0..1) for
`frontend/components/stage/Orb.tsx`'s audio-reactivity** — none of them are a hardcoded 0:
- `RealtimeEngine` computes RMS inline from each scheduled PCM chunk as it plays.
- `frontend/lib/voice/audio-player.ts` (server-side TTS — `jarvis/tts/matching.py`'s provider
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

## The orb (`components/stage/Orb.tsx`)

Jarvis's face — a single 3D sphere, centered in the app screen, reacting to
`idle`/`listening`/`thinking`/`speaking` (the same four states the voice
engines emit; `Orb.tsx` consumes them via one call from `app/page.tsx`). Built on
**three.js** — an ordinary npm dependency, bundled by the build. It is the project's one deliberate front-end 3D dependency,
chosen over a dependency-free raw-WebGL2 shader after an explicit trade-off
comparison. A custom `ShaderMaterial`'s **vertex** shader displaces an
`IcosahedronGeometry`'s surface with domain-warped fBm noise plus an outward
ripple driven by `getLevel()` (polled once per animated frame, smoothed with
an attack/release follower) — swirl for `thinking`, pulses for `speaking`,
not literal rotation of the mesh. `setState()` crossfades between four
parameter presets over ~600ms. Falls back to a CSS-gradient orb
(`.orb-fallback`, same four state classes) if three.js/WebGL fails to
initialize.

**Import `three` as a package; never hand-copy vendor files into `public/`.** The bundler resolves the
import at build time, so a missing piece is a build error rather than a blank page. A hand-served vendor
file that is missing its sibling parses and serves fine — a syntax check, `curl` and even a same-tab
`fetch()` all give zero signal — then fails at browser module-resolution time with a content-free
`TypeError: Failed to fetch dynamically imported module`, taking the entire app down silently, because
nothing in the shell runs until its static imports resolve.

- `IcosahedronGeometry`'s second argument is subdivision *detail*, not a
  segment/resolution count. Each `+1` roughly quadruples face count
  (`20 * 4^detail`) — a "high resolution" guess like `48` attempts on the
  order of `4^48` faces and hangs the renderer process, indistinguishable
  from a dead server without checking actual resource behavior. `detail: 5`
  (~20,480 faces) is already smooth at this render size; keep future tweaks
  in the single digits and sanity-check the face count before raising it.

**The mic button (`#mic-button`) is a real Mute/Unmute toggle — it never
reflects, and never touches, thinking/speaking.** Every engine exposes a real
`setMuted(bool)`/`.muted` (`engine.ts`'s shared contract) that touches
ONLY microphone capture — never `speaker`, `currentEventSource`/`ws`, or
`state`. `PipelineEngine` composes this with the self-listening suspend via
`_shouldListen()` (`active && !_recSuspended && !muted`), so muting and
"Jarvis is talking" cooperate instead of racing — muting mid-reply is
remembered and the echo-tail resume won't turn the mic back on until
unmuted. `onMicButtonClick()`'s conversation-mode branch (`frontend/app/page.tsx`) is a
plain two-way toggle (`!engine.active → start()`, else
`setMuted(!engine.muted)`) — there is no "click mic while speaking =
barge-in" shortcut, since mute-must-never-interrupt and click-to-interrupt
can't both live on one click; voice-triggered barge-in (the mic-energy
sampler above) is a separate mechanism, unaffected. `setMicVisual()` reads
`engine.muted` directly (not inferred from `state`), since mute is a
persistent flag independent of what Jarvis is doing — and because
`setMuted()` deliberately never emits a `'state'` event, callers must repaint
by calling `setMicVisual(engine.state)` themselves right after toggling it.

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
describe it in words — see `jarvis/tools/CLAUDE.md`'s own entry on the attachment
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
