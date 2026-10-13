# Handoff: Jarvis UI redesign, Phase 1 (voice)

Written 2026-10-13 by the "content ui" Claude session, for the next session to continue.

This file lives at the top of the `claude/ui-redesign` branch. The helper files it mentions
(`docs/voice-phase1/...`) and all the code it describes are on that branch, in the
`Jarvis-001-ui` folder. A copy of this file is also kept at the top of the main `Jarvis-001`
folder so it is easy to find; that copy is not part of the other branch.

- **Branch:** `claude/ui-redesign`
- **Worktree:** `C:\Users\HP\Documents\CLAUDE PROJECT\Jarvis-001-ui` (the main folder `Jarvis-001`
  is checked out on `claude/own-browser` by another session; do not commit there)
- **Design source:** `C:\Users\HP\Downloads\My Jarvis interface\design_handoff_jarvis_ui`
  (`DECISIONS.md` wins over `README.md`)
- **Memory files to read first** (in `~/.claude/projects/C--Users-HP-Documents-CLAUDE-PROJECT-Jarvis-001/memory/`):
  `feedback-findings-before-decisions.md`, `jarvis-redesign-decisions-2026-10-12.md`,
  `jarvis-test-env-windows.md`, `concurrent-claude-sessions.md`

## How the user wants this worked

- Implement approved work step by step. Ask a focused question (selectable options, a
  recommendation) only when the current step needs a decision. Do not stall unrelated work.
- Never invent requirements, silently decide, or remove an existing component without evidence
  and approval. The prototype is the source of truth for look and behaviour.
- Measure latency and report real numbers. Test the real end-to-end behaviour before moving on.
- Explain in plain language; the user is non-technical.

## Decisions in force (do not reopen)

| Topic | Decision |
|---|---|
| Full Duplex | **Kept.** (An earlier "delete it" decision was reversed.) |
| Voice Mode | Its **own section** in quick settings and Settings → Voice, using the **same dropdown** as the provider rows. Listening and Speaking dropdowns list providers only. |
| Realtime voice | **Build it in this phase**, provider-flexible, after researching providers, free tiers, local options, costs and limits. Not started. |
| Browser speech recognition | **Removed.** Recognition is on-device (Moonshine) or the chosen cloud service. |
| Local recogniser | Moonshine **Base** first; compare with Tiny on the user's **real voice** before finalising. |
| Wake engine | **Stay with openWakeWord.** One trained model per phrase. Missing defaults are trained with OWW's notebook on Colab; the user runs it. |
| Wake phrases | Four defaults all active: Hey Jarvis, Hello Jarvis, Wake up Jarvis, Jarvis wake up. Add, remove, replace, restore. |
| Local voice | Offer **both Piper and Kokoro**; the user will test and may drop one. Kokoro downloads only when chosen. |
| Interrupt | Visible while thinking, working or speaking; hidden only when idle. Stops promptly, starts nothing further, never pretends to undo. |
| Mic | Click starts a session when none runs; Esc ends it. Timings stay 30 s first listen, 7 s follow-up. |
| Push-to-talk | Space release returns to a resting state showing "Hold Space to talk". **Layout waits for the user's diagram.** |
| Model downloads | On demand, with progress; manage memory. |

## Step plan and status

| # | Step | Status |
|---|---|---|
| 1 | Interrupt visibility + Stop starts no further action | **Done, pushed** (`cb7d681`) |
| 2 | Wake phrases: one OWW model per phrase, per-phrase status, add-model upload | Code done, tests pass; committed as WIP in this handoff commit |
| 3 | One shared mic, recognition on the server (local Moonshine or Deepgram), browser recognition removed | Code done; unit and browser tests pass; **real end-to-end probe fails, undiagnosed** (see below) |
| 4 | Voice settings rebuilt (Voice Mode section; Listening/Speaking providers; Piper/Kokoro) | Not started |
| 5 | Realtime voice | Not started (research first) |
| 6 | Push-to-talk layout | Blocked on the user's diagram |

`frontend/out` is **not** rebuilt on this branch. Unfinished work is tested from
`.preview-build` (see "Running things"). Rebuild and commit `out/` only when a stage is finished.

## What was built

### Step 1 (pushed, `cb7d681`)
- `useAssistant.ts`: `working = busy || thinking || tool_running || speaking`; Home's Interrupt
  ring, phone Stop and Esc follow it.
- `conversation/orchestrator/pipeline.py`: in the tool loop, once `cancel` is set the remaining
  calls of that step are not started; each still gets a result
  (`"Not started: stopped before it ran."`). **The browser session was told to keep this check
  when it reworks that loop.**
- Tests: `test_orchestrator.py::test_stopping_between_actions_...`,
  `test_shell_e2e.py::test_interrupt_shows_while_jarvis_thinks_and_stops_the_reply`.

### Step 2 (wake phrases)
- `speech/voice/wake.py` rewritten: `WakeDetector(phrases=...)`, `set_phrases()`, `phrase_states()`
  (`ready` / `loading` / `needs_model`), `add_model(name, bytes)`, `model_file(phrase)`.
  A phrase's model is `data/wakeword/<slug>.onnx` or `<slug>_v*.onnx`
  (`slug("Wake up Jarvis") == "wake_up_jarvis"`).
- `_openwakeword_model()` imports OWW **without scikit-learn** (stub for
  `openwakeword.custom_verifier_model`); measured 14 s → 6.9 s import under load.
- A detector that once found no files now looks again when files appear (no restart needed).
- `server/jarvis/routes/voice.py`: the wake socket reads `wakePhrases` from prefs (falls back to
  the four defaults; **not** added to `prefs.DEFAULTS`, whose shape is pinned by contract fixture
  0008), waits up to 8 s for a rebuild, and `POST /api/voice/wake/models?name=x.onnx` (raw body).
- Front end: `usePrefs.ts` defaults, `wake-listener.ts` (`heard`, `restart()`), `HomeScreen.tsx`,
  `QuickSettings.tsx` (chips with `data-state`, "needs its model", "Add a phrase model…",
  polls every 2 s while loading), `api.ts` (`voice.status`, `voice.addWakeModel`).
- Tests: `tests/test_wake_phrases.py` (real-model tests need `JARVIS_WAKE_TEST_MODELS`),
  two browser tests in `test_shell_e2e.py`.

### Step 3 (shared mic, server recognition)
- `speech/speech_models.py` (new): catalogue and on-demand download with progress into
  `data/speech/<id>/`; interlock `JARVIS_SPEECH_MODEL_DOWNLOAD` (added to `main.py`).
- `speech/stt/local.py` (new): `LocalSession` (Silero VAD + Moonshine through sherpa-onnx),
  emits `speech_started`, `transcript`, and `utterance_end` after 1 s of quiet; shared recogniser
  unloads after 10 idle minutes.
- `server/jarvis/routes/realtime.py`: `/api/duplex` picks the recogniser from pref
  `listeningProvider` (`local` default, or `deepgram`); sends `downloading` / `loading` / `ready`;
  no browser fallback any more. Local model pref: `localListeningModel`.
- `pyproject.toml`: adds `sherpa-onnx>=1.13,<2`.
- Front end: `lib/voice/recognition-socket.ts` (new, shared client); `pipeline-engine.ts` uses
  `MicStream` + the socket (turn-taking unchanged); `duplex-engine.ts` lost its browser path and
  reports a start failure after `stop()`; `dictation.ts` rewritten on the same socket;
  `engine.ts` event `listening_progress` replaces `stt_fallback`;
  `speech-recognition.d.ts` deleted.
- Tests: `tests/test_local_listening.py` (downloader against a real local HTTP server; socket;
  real-recogniser test needs `JARVIS_TEST_SPEECH_MODELS`), `tests/test_voice_edit_e2e.py`
  (stand-in recogniser is now on the server), updated mic tests in `test_shell_e2e.py`.

## Measurements so far (i5-7300U, 8 GB, usually 90–100% busy from other sessions)

| What | Result |
|---|---|
| OWW "Hey Jarvis" model on 22 synthetic recordings | Hey/Hello Jarvis 4 of 4; Wake up Jarvis / Jarvis wake up 2 of 4; 0 false wakes on 14 other sentences; 7.6% of one core; +40 MB |
| OWW load | 2.8 s quiet, 7.8–14 s under load |
| Moonshine Base | 1 real word error in 16 sentences ("Summer rise"), plus "3:30" for "three"; load 14 s under load; +200 MB |
| Moonshine Tiny | Heard "Hey Jarvis" as "Page Argus" once; load 2.8 s; +70 MB |
| Voice stops → words ready (real-time feed, under load) | Base 0.9–1.4 s, Tiny 0.7–0.9 s (0.5 s of it is the VAD's end-of-speech wait, `MIN_SILENCE_S`) |
| Piper (lessac medium, fp32, 2 threads) | First audio 0.3 s; 3× real time; +117 MB |
| Kokoro (fp32) | 6.5–15 s per sentence; 3–5× slower than speech; DirectML fails on its ConvTranspose; +430 MB |
| Download sizes | sherpa-onnx 28 MB; Moonshine Base 111 MB, Tiny 30 MB; VAD 0.6 MB; Piper 67 MB; Kokoro 320 MB |
| Interrupt latency | **Not measured** (the measuring run was killed by low memory) |

## Test status at handoff

- Full Python suite (before step 3's front-end work): 2014 passed, 8 failed. Six are the known
  Windows desktop tests; one is `test_durable_crash` (uses `signal.SIGKILL`, Windows-only);
  one was my TLS rule violation, since fixed.
- `tests/test_local_listening.py`, `test_wake_phrases.py`, `test_voice.py`, `test_health.py`,
  `test_contract.py`, `test_tls_shared.py`, `test_layout.py`, `test_architecture.py`: pass.
- Browser: Interrupt, wake-phrase, mic-engine and both voice-edit tests pass on the preview build.
- `npm run typecheck`: passes.
- **Not rerun after the last edits:** the full Python suite and the full `test_shell_e2e.py`.

## Outstanding issue: the real end-to-end probe fails

`docs/voice-phase1/probe_real_listening.py` plays a recorded sentence through Chromium's fake
microphone into Speak and listen, with real Moonshine on the server and a stub model. It timed
out after 120 s waiting for the reply. **Cause unknown.** The parts are each proven separately
(socket test with real audio passes; engine test with a server stand-in passes), so the suspects
are, in order:
1. Chromium's fake-mic file playback level or format versus the VAD threshold (the file is
   16 kHz mono; Chromium resamples to 48 kHz and applies noise suppression and gain control).
2. The engine's `SilenceWatcher` never seeing silence because the file loops.
3. Model load time inside pytest (imports are very slow under pytest's import hook here).

To diagnose: wrap the wait in try/except and print `window.__marks` (status changes), the
transcript, and server-side `local._shared` / the session's `decode_ms`; log every socket frame
with `page.on("websocket", ...)`.

## Waiting on the user

1. **Voice recordings.** `docs/voice-phase1/record_my_voice.py` (12 lines, 5 s each, saved to a
   `myvoice` folder next to the script). Needed to compare Moonshine Base and Tiny and to check
   the "Hey Jarvis" model on their voice. Not done yet.
2. **Wake models.** `docs/voice-phase1/jarvis_wake_training.ipynb` on Google Colab (T4 GPU, Run
   all) produces `jarvis_wake_models.zip` with `hello_jarvis.onnx`, `wake_up_jarvis.onnx`,
   `jarvis_wake_up.onnx`. **Untested by me**; a setup cell may fail on today's Colab. Install the
   files through the editor's "Add a phrase model…" or copy them into `data/wakeword/`.
3. **Push-to-talk layout diagram.**

## Things to tell the user that have not been said yet

- With both modes hearing through the server, Speak and listen and Full duplex now differ only
  in turn-taking (Speak and listen: sentence-ending wait plus 30 s / 7 s windows; Full duplex: 1 s
  of quiet from the recogniser). Relevant to the Voice Mode section in step 4.
- The "Hey Jarvis" model also wakes on "Hello Jarvis" (and reports it as "Hey Jarvis"), so
  removing "Hello Jarvis" from the list will not stop it waking while "Hey Jarvis" stays.
- Words appear only after a pause with the on-device recogniser (no live partial text).
- The "Ask Jarvis" dock on other pages has no Interrupt control; the prototype does not show one.
- The phrase editor says "needs its model", not "training", because Jarvis cannot know whether
  a training run is happening.
- Loading the wake model no longer imports scikit-learn.
- Composer dictation now uses the same server recognition.

## Exact next steps

1. Diagnose and fix the end-to-end probe above; record click → reply timings and confirm the
   microphone tracks stop on Esc.
2. Rerun the full Python suite and `test_shell_e2e.py` + `test_voice_edit_e2e.py` (two separate
   runs); fix anything new.
3. Measure Interrupt latency (click → control hidden) and mic start/stop.
4. Report steps 1–3 to the user with the numbers, then start step 4. The agreed layout:
   LISTENING toggles → **VOICE MODE** (dropdown) → **VOICE PROVIDERS** (Listening; Speaking
   provider → AI voice model → Voice, Voice only when the model has several) → Quiet Hours.
   `speech/voice/options.py` must list real listening providers
   (`On this computer`, Deepgram when keyed) instead of engines; prefs: `voiceEngine` (mode),
   `listeningProvider`, `voiceOutput`. Add Piper and Kokoro as local voices through
   `speech_models` and a new local TTS provider in `speech/tts/`.
   Ask the user how Realtime should appear only if it is still unbuilt when step 4 ships
   (today it is listed, disabled, with its reason).
5. Step 5: research Realtime providers (Gemini Live, OpenAI Realtime, local options), costs,
   free tiers and limits; present findings and a provider-flexible design before building.
   The model layer (`intelligence/models/`) belongs to another session; coordinate.

## Coordination with other sessions

- **"Jarvis web broswer"** (branch `claude/own-browser`) is adding approval auto-resume: a
  `resume` query param in `routes/turn.py`, `resume_of` in `pipeline.py`'s `run_turn`, and a
  `resumeOf` option on `send()` in `useAssistant.ts`. Agreed on 2026-10-13: it makes those edits
  itself and keeps the cancel check in the tool loop; no overlap with this branch's edits.
- `frontend/out` will conflict between branches; whoever merges second rebuilds it.
- A step-boundary event in `pipeline.py` (separate assistant steps as separate message blocks)
  is planned for the later messaging phase and is **not** started.

## Running things on this machine

```bash
WT="C:/Users/HP/Documents/CLAUDE PROJECT/Jarvis-001-ui"
S="<a scratch folder>"                      # holds sc/sitecustomize.py, sherpa/, models
export JARVIS_DATA_DIR="$S/data-ui-test"    # ALWAYS export a scratch data dir first
export PYTHONPATH="$WT/server;$S/sc;$S/sherpa"   # worktree code first; sc = global site-packages; sherpa = scratch install
export JARVIS_FRONTEND_DIR="$WT/.preview-build"
export JARVIS_WAKE_TEST_MODELS="$S/wake-models"          # hey_jarvis_v0.1.onnx, melspectrogram.onnx, embedding_model.onnx
export JARVIS_TEST_SPEECH_MODELS="$S/speech-test-models" # moonshine-base-en/, silero_vad.onnx, sample.wav
"C:/Users/HP/Documents/CLAUDE PROJECT/Jarvis-001/.venv/Scripts/python.exe" -m pytest tests/... -q -p no:cacheprovider
cd "$WT/frontend" && JARVIS_BUILD_DIR=../.preview-build npm run build
```

- The venv's `jarvis` is an editable install of the **main** folder; without the worktree's
  `server` first on `PYTHONPATH` the tests run the wrong code.
- sherpa-onnx is **not** in the venv (do not pip install into it). It was installed into a
  scratch folder with `pip install --target`. The scratch folder used this session was
  `%TEMP%\claude\C--Users-HP-Documents-CLAUDE-PROJECT-Jarvis-001\6d8c5d15-...\scratchpad`; if it
  is gone, reinstall there and re-download models (URLs are in `speech/speech_models.py`; wake
  models can be copied read-only from the main folder's `data/wakeword/`).
- `frontend/node_modules` in the worktree is a junction to the main folder's.
- The laptop is short of memory and CPU. Run browser tests in small groups, never alongside
  another heavy run; background runs get killed when memory is low. Deselect
  `tests/test_tools.py::test_open_app_refuses_honestly_off_windows` (it launches Notepad).
- Bash heredocs containing certain quote patterns fail here; write edit scripts to a file first.
