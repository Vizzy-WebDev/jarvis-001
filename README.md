# Jarvis

A local, voice/text personal assistant. Node.js + Express server, plain ES-module
front-end (no build step, no framework), and it only ever listens on
`127.0.0.1` — nothing about it is reachable from anywhere else on your network.

For plain-language usage instructions, see **[`How to Use Jarvis.md`](How%20to%20Use%20Jarvis.md)**.
This file is the technical overview.

## Requirements

- **Node.js 24+** (Chat History and Memory use Node's built-in `node:sqlite` — no
  separate database install).
- **Windows.** Computer control, screen recording, and a few OS-level checks are
  Windows-specific; the rest is cross-platform in principle but only tested here.
- **Google Chrome or Microsoft Edge** for voice input (Firefox doesn't support the
  Speech Recognition API Jarvis's default voice pipeline uses — typing still works
  everywhere).
- **ffmpeg** on `PATH`, only if you want screen recording (`take_screenshot` and
  everything else works without it).

## Quick start

```
Start Jarvis.bat        # what you'd normally double-click: npm install (if needed),
                         # launch, open your browser
```

or, for development:

```
npm install
npm start
```

The server listens on `127.0.0.1:3000`. The first run asks for a free Google Gemini
API key (get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey))
— that's the only hard requirement; Anthropic, OpenAI, a local model server, or any
OpenAI-compatible gateway can all be added afterward from Model Settings.

## What it can actually do

- **Real-time conversation, by voice or text.** Two independent voice engines behind
  one interface: an any-model pipeline (browser speech-to-text → whichever AI model
  is active → server-side or browser TTS) and Gemini Live (near-instant, mid-word
  interruptible, Gemini only). Adaptive turn-taking tells "still thinking" pauses
  apart from "actually done talking."
- **Any model, real fallback.** Connect Gemini, Claude, OpenAI, a local server
  (Ollama/LM Studio), or any other OpenAI/Anthropic/Gemini-shaped gateway (OpenRouter,
  Groq, Together, an in-house endpoint) via a generic Custom connection that probes
  the address to work out what's actually on the other end. Auto-routing picks a
  model per task and by real, measured cost, not just a name-based guess; a broken
  model degrades gracefully with the conversation's context intact.
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
- **Real file generation.** Word, Excel, PowerPoint, and plain data/text files, built
  for real and mechanically verified (re-opened through Jarvis's own document reader)
  before being handed to you, served as a forced download.
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
- **Self-improvement and self-knowledge.** Jarvis reviews its own completed work,
  extracts lessons, and — only from its own directly-observed track record, never
  from something merely read online — can turn a genuinely recurring one into a
  behaviour rule, always undoable. Separately, it can give an honest, evidence-backed
  answer to "can you actually do this reliably," "has anything gone wrong with you
  lately," and "how much have I spent" (three separately-labelled numbers: measured,
  provider-reported, calculated — never blended into a guess).
- **Operational self-awareness.** Automatic self-diagnosis with real self-heal for a
  handful of failure modes, live rolling CPU/reachability awareness ("can you handle a
  heavy task right now"), and mechanical + semantic verification wired into artifact
  creation, background Jobs, and scheduled-task outcomes.

## Honest limits

- No wake word — you start it with a click or the Space bar.
- No photographic/raster image generation — no adapter in use does this.
- A generated `.pptx` slide deck is checked less thoroughly than `.docx`/`.xlsx` (its
  master/theme chain can't be round-tripped the same way) — worth a look in real
  PowerPoint before fully trusting one.
- Runs only while its own server window is open; nothing scheduled fires into an
  empty room while it's closed — it catches up, clearly marked "ran late," next time
  you open it.
- Semantic answer-verification is wired at three completion surfaces (artifacts, Job
  completion, scheduled-task outcomes) but deliberately **not** into every live chat
  answer — the costliest, most frequent surface on a routinely rate-limited model
  roster, a disclosed cost/scope decision rather than an oversight.

## Configuration and data

- **`.env`** (git-ignored) holds every API key/secret. It's written by
  `server/config.js` — never hand-edit its format.
- **`data/`** (git-ignored) holds JSON state (models, connections, prefs, tasks) plus
  `jarvis.db`, a SQLite database (Chat History, Memory, Jobs, Self-Improvement,
  Self-Model).
- For an isolated test run, three env vars redirect everything: `JARVIS_DATA_DIR`,
  `JARVIS_ENV_PATH`, and `PORT`. Your real `data/`, `.env`, and port 3000 are never
  touched by a test unless you explicitly point at them.

## Architecture

```
server/
  server.js       Express app, all routes, binds 127.0.0.1 only
  adapters/        One module per wire format: Anthropic, Gemini, OpenAI-compatible
  models/          Connections/models registry, routing, health, execution
  conversation.js  Neutral, model-agnostic transcript format
  db.js            The one SQLite connection + migrations (Chat History, Memory, Jobs, ...)
  chat-store.js    Conversation/message CRUD + full-text search
  memory/          Memory Manager: extraction, approval policy, checkpoints
  scheduler/       Scheduled Tasks, recurrence, briefing
  jobs/            Background Task Orchestration ("Jobs")
  heartbeat/       Proactive attention — noticing things, deciding whether to speak up
  improvement/     Self-Improvement — Jarvis reviewing and adjusting its own behaviour
  self/            Self-Model — grounded, evidence-backed self-knowledge
  ops/             Self-diagnosis + self-heal, environment awareness, verification
  cost/            Automatic spend/usage tracking, fed back into model routing
  artifacts/       Real file generation (.docx/.xlsx/.pptx/...), verified on creation
  control/         Computer control: perceive/decide/act loop, screen capture, safety
  connectors/      MCP/API/CLI/browser/files app connectors
  tools/           Auto-loaded executable capabilities (weather, open_app, run_code, ...)
  skills/          Folder Skills (SKILL.md-based instructions) — install/store logic
  capabilities.js  The composition seam: tools + Skills + connectors -> one invoke()
  sandbox/         Isolated code execution backends
  monitor/         "Watch for X, then act" background checks
public/
  app.js           UI shell, drawer/router, stream-event handling
  nav.js           SECTIONS registry — the single source of truth for the drawer/router
  screens/         One file per drawer section
  engines/         PipelineEngine (any model) / LiveEngine (Gemini Live)
  orb.js           The 3D orb (idle/listening/thinking/speaking)
```

See `CLAUDE.md` for the full module-by-module design record (this file is the
condensed version); most subdirectories also carry their own `CLAUDE.md`.

## No automated test suite — how to verify a change

There's exactly one `npm` script (`start`). Verification is manual and deliberate:

- `node --check <file>` across every changed file (also catches an accidental
  `require()` inside an ES module).
- Boot a real, throwaway instance — `JARVIS_DATA_DIR`/`JARVIS_ENV_PATH`/`PORT` pointed
  at scratch values — via a background process, and confirm it starts cleanly, runs
  its migrations, and loads every tool with no error.
- `curl` against real routes directly.
- Pure-logic modules can be exercised with a one-off `node --input-type=module -e
  "..."` script, no server needed.
- To verify what a model actually *did* (not what it claimed), read the real
  `toolCalls`/`toolResults` straight out of the `messages` table in `data/jarvis.db`
  (read-only) — a model's own narration of success/failure isn't reliable evidence on
  its own.

## Security posture

Binds `127.0.0.1` only. Secrets live in `.env`, never in `data/`. Any route serving
content that could contain arbitrary text a model or user supplied (generated
artifacts, uploads) forces `Content-Disposition: attachment`, `X-Content-Type-Options:
nosniff`, and a sandboxing Content-Security-Policy — unconditionally, never behind an
optional query flag.

## More

- **[`How to Use Jarvis.md`](How%20to%20Use%20Jarvis.md)** — for using it, no technical background needed.
- **[`CLAUDE.md`](CLAUDE.md)** — the full architecture and design-decision record.
- **[`handoff.md`](handoff.md)** — session-by-session build history.
