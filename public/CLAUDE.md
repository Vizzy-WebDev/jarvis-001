# Front-end (`public/`)

Plain ES-module front-end, no build step, no framework. `app.js` is the shell;
`nav.js`'s `SECTIONS` array is the single source of truth for the drawer, the
hash router (`router.js`), and voice navigation (`open_section` skill) — add a
capability by adding one entry here plus one file in `screens/`. Screens are
hash-routed (`#/models`) and each exports `async function render(container)`
that wipes and rebuilds its container from scratch on every change — no
partial DOM patching anywhere.

`screens/_modal.js` is the one popup component (add-a-model, create-a-task,
add-a-briefing-source all use it). **It must be appended to `<body>`, never to
a screen's own container** — every screen's `render()` starts with
`container.innerHTML = ''`, so a modal living inside one would be destroyed by
any re-render that isn't the modal's own close. `api.setSubmitLabel()`
persists a `currentLabel` so a later `setBusy(false)` doesn't silently revert
a relabel (e.g. "Test and add" -> "Add selected" after a discovery checklist
appears) back to the dialog's original button text.

## Voice engine (`public/engines/*.js`)

`PipelineEngine` (Chrome STT -> any model -> TTS) and `LiveEngine` (Gemini
Live, audio in/out over WebSocket). Both extend `VoiceEngine`, which provides
`.on(event, handler)`/`._emit()`. Events: `state`, `transcript`, `chunk`,
`tool`, `tool_result`, `model_switch`, `restart`, `paused`, `done`, `error`.
`app.js` wires UI to whichever is selected in settings and doesn't otherwise
care which engine it's talking to.

**All three voice-output paths now expose a real `getOutputLevel()` (0..1) for
`orb.js`'s audio-reactivity** — none of them are a hardcoded 0 any more:
- `LiveEngine` computes RMS inline from each scheduled PCM chunk as it plays.
- `audio-player.js` (server-side TTS — `server/tts/index.js`'s provider
  registry, e.g. ElevenLabs; the free `browser` voice is `PipelineEngine`'s
  actual default, not this) reads an
  **offline-decoded amplitude envelope** (`voice-envelope.js`'s
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
- `browser-speaker.js` (the `browser` `speechSynthesis` voice-output setting)
  has no analysable audio to read at all, so its `getOutputLevel()` is
  timing-derived instead: each `onboundary` word event re-triggers a short
  decaying pulse (~220ms), giving the orb a real per-word rhythm rather than
  flat procedural motion.

`getMicLevel()` is real on both engines: `PipelineEngine` reads
`MicLevelMonitor`; `LiveEngine` computes RMS inline from each
`onaudioprocess` frame.

**The mic never re-enters while Jarvis is talking, on purpose.**
`pipeline-engine.js`'s continuous `SpeechRecognition` opens its own separate,
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
(`turn-detector.js`) is reset via `reset()` at the top of every
`_startBargeInSampler()` run — it must never carry a stale timestamp across
turns, or the barge-in sustain gate can trigger almost instantly on the
*next* reply.

**Don't gate `LiveEngine`'s mic upload for echo reasons the way
`PipelineEngine` is gated above.** Gemini Live's own barge-in depends on its
server-side voice detection hearing the user while it's talking; gating
`onaudioprocess` during Jarvis's own playback would silently disable that
entirely, since Gemini can't detect being talked over in audio it was never
sent. `LiveEngine` streams mic audio continuously and lets Gemini's own
`interrupted` event handle it — one duplex socket, not two separate
capture/playback pipelines like the Pipeline engine.

**"Jarvis's voice just stops" has three unrelated causes** — check which one
actually matches: (1) a failed `/api/tts` fetch resolving silently to `null`
with no retry — now retries once. (2) A mid-stream chat-stream error not
calling `speaker.end()` — now every error path calls it. (3) Chrome's
`speechSynthesis` silently dropping an utterance with neither `onend` nor
`onerror` firing — now has a per-utterance watchdog that force-continues if
Chrome never confirms.

## The orb (`public/orb.js`, `public/vendor/three/`)

Jarvis's face — a single 3D sphere, centered in the app screen, reacting to
`idle`/`listening`/`thinking`/`speaking` (the same four states the voice
engines emit; `orb.js` consumes them via one call, `app.js`'s
`setMicVisual()`). Built on **vendored three.js**
(`public/vendor/three/three.module.min.js`, pinned 0.185.1, pulled via
`npm pack three` — the project's one deliberate front-end dependency, chosen
over a dependency-free raw-WebGL2 shader after an explicit trade-off
comparison). A custom `ShaderMaterial`'s **vertex** shader displaces an
`IcosahedronGeometry`'s surface with domain-warped fBm noise plus an outward
ripple driven by `getLevel()` (polled once per animated frame, smoothed with
an attack/release follower) — swirl for `thinking`, pulses for `speaking`,
not literal rotation of the mesh. `setState()` crossfades between four
parameter presets over ~600ms. Falls back to a CSS-gradient orb
(`.orb-fallback`, same four state classes) if three.js/WebGL fails to
initialize.

**Two vendoring facts, worth checking again on the next three.js update:**
- `three.module.min.js` alone is not a complete vendor — it imports a sibling
  `three.core.min.js` (three.js's build split, ~r150+). Missing it parses and
  serves fine (`node --check`, curl, even a same-tab `fetch()` all give zero
  signal) but fails at browser module-resolution time with a content-free
  `TypeError: Failed to fetch dynamically imported module` — and because
  `app.js` imports `orb.js` at the top level, that takes the **entire app**
  down silently (nothing in `app.js` runs until its static imports resolve).
  Only a real browser network tab shows the missing 503. Both files must ship
  together; `public/vendor/three/README.md` has the update recipe.
- `IcosahedronGeometry`'s second argument is subdivision *detail*, not a
  segment/resolution count. Each `+1` roughly quadruples face count
  (`20 * 4^detail`) — a "high resolution" guess like `48` attempts on the
  order of `4^48` faces and hangs the renderer process, indistinguishable
  from a dead server without checking actual resource behavior. `detail: 5`
  (~20,480 faces) is already smooth at this render size; keep future tweaks
  in the single digits and sanity-check the face count before raising it.

**The mic button (`#mic-button`) is a real Mute/Unmute toggle — it never
reflects, and never touches, thinking/speaking.** Both engines expose a real
`setMuted(bool)`/`.muted` (`voice-engine.js`'s shared contract) that touches
ONLY microphone capture — never `speaker`, `currentEventSource`/`ws`, or
`state`. `PipelineEngine` composes this with the self-listening suspend via
`_shouldListen()` (`active && !_recSuspended && !muted`), so muting and
"Jarvis is talking" cooperate instead of racing — muting mid-reply is
remembered and the echo-tail resume won't turn the mic back on until
unmuted. `onMicButtonClick()`'s conversation-mode branch (`app.js`) is a
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

## Delivering a real image/video into the transcript (`ui_action:{type:'attachment'}`)

A tool result can put a real, visible image or video into the current reply, not just
describe it in words — see `server/tools/CLAUDE.md`'s own entry on the `ui_action`
convention for the server side (`take_screenshot.js`/`stop_screen_recording.js`).
`app.js`'s `tool_result` handler calls `appendAttachment({kind, url, mimeType})`, which
appends a real `<img>`/`<video controls>` (class `bubble-attachment`, `style.css`) into
`currentAssistantEl` — creating one first if the attachment arrives before any reply text
has streamed in yet.

**Two real bugs this surfaced in the existing streaming-bubble code, both fixed as part
of adding this — worth knowing before touching either path again:**
- **`currentAssistantEl.textContent += text` in the `chunk` handler silently WIPES any
  already-appended attachment element.** `.textContent`'s own setter replaces every
  child with a single text node, even when read back and reassigned via `+=` — so if an
  image landed first and then more reply text streamed in on the same turn (a completely
  normal sequence: attachment now, a closing sentence after), the image vanished with no
  error. Fixed by switching to `appendChild(document.createTextNode(text))`, which only
  ever adds a sibling, never rebuilds the node.
- **The three "if the bubble ended up empty, remove it" checks** (`paused`/`done`/`error`
  handlers) used a bare `!currentAssistantEl.textContent`, which is true for an
  image-only reply — an `<img>`/`<video>` contributes nothing to `textContent` — and
  would delete a bubble that actually held a real, delivered attachment. Fixed with a
  small shared `bubbleHasContent(el)` helper (`textContent` OR a `.bubble-attachment`
  child) used by all three checks instead of the bare `textContent` test. **The general
  lesson**: once a bubble can hold something other than plain text, every "is this
  bubble empty" check anywhere in the file needs to agree on what "empty" means — a
  narrower, ad-hoc check at just one call site drifts out of sync with the others.
