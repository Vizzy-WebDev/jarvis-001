# Jarvis

A local, voice/text personal assistant. Python + FastAPI backend serving a
Next.js/React front end as one process on one port, and it only ever listens on
`127.0.0.1` — nothing about it is reachable from anywhere else on your network.

For plain-language usage instructions, see **[`How to Use Jarvis.md`](How%20to%20Use%20Jarvis.md)**.
This file is the technical overview.

## Requirements

- **Python 3.11+.** Nothing else: the front end ships pre-built, so running Jarvis
  needs no Node, no npm, and no separate database install (SQLite is built in).
- **Windows.** Computer control, screen recording, and a few OS-level checks are
  Windows-specific; the rest is cross-platform in principle but only tested here.
- **Google Chrome or Microsoft Edge** for voice input (Firefox doesn't support the
  Speech Recognition API Jarvis's default voice pipeline uses — typing still works
  everywhere).
- **ffmpeg** on `PATH`, only if you want screen recording (`take_screenshot` and
  everything else works without it).

## Quick start

```
Start Jarvis.bat        # what you'd normally double-click: first-run setup (if
                         # needed), launch, open your browser
```

or, for development:

```
cd backend
python -m jarvis.main
```

The server listens on `127.0.0.1:3000`. The first run asks for a free Google Gemini
API key (get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey))
— that's the only hard requirement; Anthropic, OpenAI, a local model server, or any
OpenAI-compatible gateway can all be added afterward from Model Settings.

## What it can actually do

- **Real-time conversation, by voice or text.** Three voice engines behind one
  interface, picked by what's actually configured: a browser-speech-to-text →
  any-model → TTS pipeline (works with anything); a keeps-listening-while-it-talks
  engine over a server-relayed recognition socket; and a provider's own realtime
  speech-to-speech session (near-instant, mid-word interruptible) when one is
  configured — nothing in the engine picker is hardcoded to a specific provider.
  Adaptive turn-taking tells "still thinking" pauses apart from "actually done
  talking."
- **Any model, real fallback.** Connect Gemini, Claude, OpenAI, a local server
  (Ollama/LM Studio), or any other OpenAI/Anthropic/Gemini-shaped gateway (OpenRouter,
  Groq, Together, an in-house endpoint) via a generic Custom connection that probes
  the address to work out what's actually on the other end. Auto-routing picks a
  model per task and by real, measured cost, not just a name-based guess; a broken
  model degrades gracefully with the conversation's context intact.
- **A real, adaptive delivery register, separate from what it concludes.** Warmth,
  directness and playfulness shift with the moment — softer when you sound genuinely
  distressed, more measured on an inherently serious topic, and it can genuinely
  laugh out loud (a real audible cue, never a transcribed "haha") when something
  actually lands. What it concludes never changes to fit the mood; only how it's said
  does.
- **Memory and Chat History.** Approved facts about you sit directly in every
  conversation (no search needed); full conversation history persists across restarts
  and is full-text searchable, including from inside a live conversation ("what did I
  say about that last month?"). A trust dial controls how much Memory can save
  without asking first — a genuine conflict with something already saved always asks,
  at every trust level.
- **Skills, and connected apps/services.** Folder-based Skills (house style,
  templates, a process to follow) install from a `.zip` or a public GitHub repo link.
  Separately, App Control connects real apps/services over MCP, API, or CLI, plus
  Jarvis's own built-in ability to actually operate your desktop — click, type,
  launch apps, read what's on screen — always with a spoken heads-up and your
  explicit OK before it takes over.
- **Screen capture.** Real screenshots and ffmpeg-recorded `.mp4` screen recordings
  delivered straight into the conversation, plus a persistent Screen Sharing mode for
  ongoing "look at what I'm doing" conversations.
- **Real file generation.** Word and Excel files, built for real and mechanically
  verified (re-opened through Jarvis's own document reader) before being handed to
  you, served as a forced download — plus plain data/text formats. It can also READ a
  PowerPoint file you hand it; it cannot create one (see Honest limits).
- **Automation that runs without you watching.** Scheduled Tasks (on a clock, always
  read back and confirmed before saving), Background Jobs (long-running work
  backgrounded from a live conversation, checkable and cancellable anytime, escalating
  to you only when a real decision is needed), and a Morning Briefing assembled from
  live sources you choose.
- **Proactive attention, used sparingly.** Jarvis can notice something worth
  surfacing on its own — a stalled background job, a time-sensitive commitment from
  Memory — and speak up first, judged case-by-case against real context rather than a
  fixed "always interrupt for this" list, and held back overnight by quiet hours
  except for a genuine emergency.
- **Self-improvement and self-knowledge.** Jarvis reviews its own completed work
  (every finished job and scheduled task run, plus anything that reads as you
  correcting it), extracts lessons, and — only from its own directly-observed track
  record, never from something merely read online — can turn a genuinely recurring
  one into a behaviour rule, always undoable. Separately, it can give an honest,
  evidence-backed answer to "can you actually do this reliably," "has anything gone
  wrong with you lately," and "how much have I spent" (three separately-labelled
  numbers: measured, provider-reported, calculated — never blended into a guess).
- **Operational self-awareness.** Automatic self-diagnosis with real self-heal for a
  handful of failure modes, live rolling CPU/reachability awareness ("can you handle a
  heavy task right now"), and mechanical + semantic verification wired into artifact
  creation, background Jobs, and scheduled-task outcomes.

## Honest limits

- No wake word — you start it with a click or the Space bar.
- No photographic/raster image generation — no adapter in use does this.
- **No PowerPoint generation.** Jarvis can read a `.pptx` you hand it, but asking it
  to create one gets a plain refusal naming exactly what it can produce instead
  (`.docx`, `.xlsx`, plain text formats) — not a lesser-effort attempt.
- Runs only while its own server window is open; nothing scheduled fires into an
  empty room while it's closed — it catches up, clearly marked "ran late," next time
  you open it.
- Semantic answer-verification is wired at three completion surfaces (artifacts, Job
  completion, scheduled-task outcomes) but deliberately **not** into every live chat
  answer — the costliest, most frequent surface on a routinely rate-limited model
  roster, a disclosed cost/scope decision rather than an oversight.

## Configuration and data

- **`.env`** (git-ignored) holds every API key/secret. It's written by
  `backend/jarvis/config.py` — never hand-edit its format.
- **`data/`** (git-ignored) holds JSON state (models, connections, prefs, tasks) plus
  `jarvis.db`, a SQLite database (Chat History, Memory, Jobs, Self-Improvement,
  Self-Model).
- For an isolated test run, three env vars redirect everything: `JARVIS_DATA_DIR`,
  `JARVIS_ENV_PATH`, and `PORT`. Your real `data/`, `.env`, and port 3000 are never
  touched by a test unless you explicitly point at them.

## Architecture

```
backend/jarvis/
  main.py          FastAPI app + routers + the static mount that serves the front end
  assembly.py      The composition root — builds the registry/orchestrator once; starts every background clock
  routes/          One module per area — a surface over the subsystems, no business logic
  capabilities/    The capability contract, the registry, and the one dispatcher
  orchestrator/    pipeline.py — the turn loop
  personality.py   The adaptive delivery register — tone floors, sticky style, real vocal laughter
  gateway/         Connections/models registry, routing, availability, probing
  adapters/        One module per wire format: Anthropic, Gemini, OpenAI-compatible
  policy/          The permission layer — decided independently of model behaviour
  events/          Typed event bus; observers/ subscribe (cost, security, verification, improvement)
  conversation.py  Neutral, model-agnostic transcript format
  db.py            The one SQLite connection; migrations.py + migrations_extra.py hold the schema
  chat_store.py    Conversation/message CRUD + full-text search
  memory/          Memory Manager: extraction, approval policy, checkpoints
  scheduler/       Scheduled Tasks, recurrence, briefing
  jobs/            Background Task Orchestration ("Jobs")
  heartbeat/       Proactive attention — noticing things, deciding whether to speak up
  improvement/     Self-Improvement — Jarvis reviewing and adjusting its own behaviour
  self/            Self-Model — grounded, evidence-backed self-knowledge
  ops/             Self-diagnosis + self-heal, environment awareness, verification
  cost/            Automatic spend/usage tracking, fed back into model routing
  artifacts/       Real file generation (.docx/.xlsx/plain text), verified on creation
  control/         Computer control: perceive/decide/act loop, screen capture, safety
  connectors/      MCP/API/CLI/browser/files app connectors, the catalogue, OAuth
  tools/           Auto-loaded executable capabilities (weather, open_app, run_code, ...)
  skills/          Folder Skills (SKILL.md-based instructions)
  sandbox/         Isolated code execution backends
  monitor/         "Watch for X, then act" background checks
  documents/       Reads .docx/.xlsx/.pptx into Markdown (write support: docx/xlsx only)
  voice/ tts/ stt/ Voice options, speech synthesis and recognition provider seams
backend/tests/     ~1300 tests, plus contract/fixtures/ — 45 recorded HTTP exchanges
frontend/
  app/page.tsx     The shell: stage, orb, conversation panel, composer, drawer, router
  components/      screens/ (one per section), ui/ (shared primitives), conversation/
  lib/             api.ts (the one typed client), nav.ts (the SECTIONS registry), voice/ (three engines)
  out/             The BUILT export the backend serves — committed, so running needs no Node
```

See `CLAUDE.md` for the full module-by-module design record (this file is the
condensed version); most subdirectories also carry their own `CLAUDE.md`.

## How to verify a change

```
cd backend && python -m pytest tests -q                      # ~1300 tests
cd backend && python -m pytest tests/test_shell_e2e.py -q     # 65, in a real browser
cd frontend && npm run typecheck && npm run build             # only if the UI changed
```

Run the first two as separate commands, not combined into one invocation — running
the whole backend suite and the browser suite in a single pytest process has produced
spurious failures in the browser tests that go away the moment the same test is
re-run on its own.

Three layers, each catching what the others cannot:

- **Unit and integration tests** over the real modules.
- **The contract harness** replays 45 real HTTP exchanges recorded from the Node
  implementation this replaced — the durable record of the behaviour promised before
  the rewrite, which outlived the implementation it was recorded from.
- **Playwright** drives the built front end in a real browser against a real backend
  on a scratch port, which is what catches a screen that renders but never calls its
  route.

Tests never touch your real `data/`, `.env` or port: `JARVIS_DATA_DIR`,
`JARVIS_ENV_PATH` and `PORT` are wired through the shared fixtures. To verify what a
model actually *did* (not what it claimed), read the real tool calls and results
straight out of the `messages` table in `data/jarvis.db` — a model's own narration of
success or failure isn't reliable evidence on its own.

## Security posture

Binds `127.0.0.1` only. Secrets live in `.env`, never in `data/`. Any route serving
content that could contain arbitrary text a model or user supplied (generated
artifacts, uploads) forces `Content-Disposition: attachment`, `X-Content-Type-Options:
nosniff`, and a sandboxing Content-Security-Policy — unconditionally, never behind an
optional query flag.

## More

- **[`How to Use Jarvis.md`](How%20to%20Use%20Jarvis.md)** — for using it, no technical background needed.
- **[`CLAUDE.md`](CLAUDE.md)** — the full architecture and design-decision record.
- **[`handoff-archive.md`](handoff-archive.md)** — session-by-session build history.
