# Pre-migration audit — findings and decisions

Every finding here was verified against the running code or the real source, not
against comments or `CLAUDE.md`. Where a claim could be executed, it was. Findings
that turned out to be my own test error are not listed; several were.

Format: **Finding → Evidence → Impact → Recommendation → Risks.**

---

## A. Product-requirement failures (the app does not do what it claims)

### A1. A background Job cannot produce a file — BROKEN

**Evidence.** `tools/create_artifact.js:70` sets `meta: true`. `models/runner.js:335`
calls `getToolDeclarations({ includeMeta: !opts.background })`. `jobs/worker.js:129`,
`scheduler/scheduler.js:58` and `scheduler/briefing.js:213` all set `background: true`.

**Impact.** "Research the market and write me a report" is structurally impossible for
the Jobs subsystem — the tool is stripped from the declaration list before the model
ever sees it. A scheduled task cannot produce its weekly spreadsheet. This is a direct
contradiction of the general-purpose requirement, and it is a design contradiction
rather than a bug in the writers: `meta` means "live-conversation-only" (`tools/index.js:57`),
which producing a deliverable is the opposite of.

**Recommendation.** Drop `meta: true` from `create_artifact`. Bound the risk with a
per-job artifact cap, not by hiding the tool.

**Risks.** A background Job could then create files unprompted. That is the intent.

### A2. A "PDF" is plain text, and is stamped verified — BROKEN

**Evidence.** `create_artifact.js:49-61` `MIME_BY_EXT` has no `.pdf`/`.png`;
`mimeTypeFor` (`:63-65`) falls back to `text/plain` for any unknown extension; `:119-122`
writes the model's text verbatim. `ops/verify.js:46-65` handles Office, `.json` and
`.svg`, then **returns `ok: true` for everything else** on a non-empty check alone.
`create_artifact.js:142` then calls `recordVerification(verified: true)`.

**Impact.** Ask for a PDF and you get `report.pdf` containing Markdown, served as
`text/plain`, marked `verified = 1`, with a download card. This inverts the subsystem's
own stated guarantee that a bad file is "never left behind pretending to be real". The
model is told raster images are impossible, but nothing stops it writing `logo.png`.

**Recommendation.** Reject unknown/known-binary extensions in `create_artifact` with a
message naming the real alternative; give `verifyFileOpens` magic-byte checks
(`%PDF-`, PNG/JPEG signatures, `PK`) so a sandbox-produced binary is genuinely checked.

**Risks.** The model will refuse tasks it previously "completed". That is the point, and
it needs a prompt line so the refusal is graceful (see A3).

### A3. The propose-first rule the docs locate in `prompt.js` is not there — MISSING

**Evidence.** `artifacts/CLAUDE.md` and `create_artifact.js:20-25` both state the hybrid
creation model "lives entirely in `prompt.js`'s instruction text". Grep for
`artifact|create_artifact|propose creating` across all 506 lines of `prompt.js`:
**zero matches.**

**Impact.** There is no system-prompt guidance at all on when to produce a file rather
than answer in chat, which format to choose, or that `run_code` can produce files at
all. Producing real outputs is an accident of tool-description matching, not designed
behaviour.

**Recommendation.** A short "Producing real outputs" block in the system instruction.

### A4. On the better sandbox backend, code produces no files at all — BROKEN

**Evidence.** `sandbox/restricted-backend.js:144-156` collects `outputFiles` before
cleanup. `sandbox/wsl-backend.js` has no equivalent — its return object (`:173-183`)
has no `outputFiles`, and its `finally` (`:184-189`) deletes the directory.
`tools/run_code.js:114` iterates `result.outputFiles || []`.

**Impact.** `sandbox/detect.js:49-53` prefers WSL whenever a distro exists, and
`:71-78` actively tells the user to install it. So following Jarvis's own advice
**silently stops file production from code**. The failure is invisible — the model
just sees stdout with no artifacts.

**Recommendation.** Make `outputFiles` part of the backend contract in `sandbox/runner.js`
rather than a per-backend extra.

**Risks.** Cannot be tested on Linux; WSL is Windows-only and `wsl-backend.js:5-12`
states it has never been run against a real distro.

### A5. `analyze_spreadsheet` and `run_skill_script` discard every file they produce

**Evidence.** `run_skill_script.js:91-110` and `analyze_spreadsheet.js:102-151` return
only stdout. `analyze_spreadsheet.js:43-48` explicitly instructs the model to end with
"exactly one `console.log()`".

**Impact.** This is precisely the "analyse a CSV and produce a chart" case.
`analyze_spreadsheet` already places the sheet as `data.csv` beside a model-written
script — the ideal position to emit a chart — and then deletes whatever it wrote.

**Recommendation.** One shared `artifacts/capture.js`, called by all three tools.

---

## B. Model subsystem — the owner has already called for fundamental redesign

### B1. OpenRouter cannot work — BROKEN

**Evidence.** `models/probe.js:97` sets `keyRequired: Boolean(secret)` — the probe tests
*listing*, never generation. OpenRouter's catalogue is public; this repo proves it
(`cost/prices.js:70` fetches it with no auth header).

**Impact.** Adding OpenRouter without a key succeeds, saves keyless, reports
`configured: true`, wins routing, then 401s on the first real turn — classified `auth`,
banning the connection for **6 hours** (`health.js:32`).

**Recommendation.** Prove generation with one minimal completion, not a listing.
Store `authProven`.

### B2. `"mini"` is a substring of `"gemini"` — BROKEN

**Evidence.** `models/catalog.js:68` `FAST_HINTS = /flash|haiku|mini|nano|lite|small|luna/i`.
Verified: `gemini-2.5-pro` and `gemini-3-pro` both match.

**Impact.** Flagship *quality* models are scored as *fast* models, distorting every
routing decision on the owner's primary provider.

**Recommendation.** Word-boundary anchoring, and prefer listing evidence over name
guessing.

### B3. OpenRouter cost tracking is unreachable dead code — BROKEN

**Evidence.** `models/providers.js` defines exactly five provider ids
(`openai, anthropic, gemini, local, custom`). `cost/prices.js:65` and
`cost/balances.js:84` both gate on `c.provider === 'openrouter'` — a value the system
cannot produce.

**Recommendation.** Key prices and balances by `connectionId`, which is already unique,
stored and hydrated. **Open decision: this needs a migration of existing cost history.**

### B4. A credential can be sent to the wrong vendor — BROKEN (security)

**Evidence.** `gemini-key.js:31-36` returns the secret from *any* connection whose
`adapter === 'gemini'`. `turn-check.js:24` and `live.js:70` construct
`new GoogleGenAI({ apiKey })` with **no `baseUrl`**. `probe.js:169` stores
`adapter: 'gemini'` plus a non-Google `baseUrl` when the Gemini shape is detected.

**Impact.** A Custom connection resolved to the Gemini shape against a non-Google host
has its key sent to `generativelanguage.googleapis.com`.

**Recommendation.** Return `{apiKey, baseUrl}` and thread `baseUrl` through every
`GoogleGenAI` construction; require `kind === 'first-party'` before treating a
connection as a Google key.

### B5. The adapter capability ceiling is a hard gate — INCOMPLETE BY DESIGN

**Evidence.** `adapters/openai-compatible.js:29` declares
`{video:false, audio:false, vision:true, webSearch:false}`; `ai.js:41-49` `meetsNeed()`
requires the adapter ceiling **and** the model's own caps.

**Impact.** No model behind the openai-compatible adapter can *ever* serve vision,
video, audio or webSearch. Content Analysis, research grounding and monitoring are
permanently Gemini-only. This is the largest single obstacle to model-agnosticism.

**Recommendation.** Separate "the adapter has code for this" (a real gate, already
checked correctly at `content/intake.js:389` via `typeof adapter.uploadFile === 'function'`)
from "this model can do this" (a per-model fact, seeded from the ceiling but overridable).

---

## C. Verified good — preserve, do not redesign

- **The pure-policy layer.** Exhaustive truth tables run against all three: Memory's
  conflict floor holds at every trust level and `ask` uses `Infinity` so a malformed
  score of 5.0 still cannot auto-save; Improvement's three floors (tier, kind, conflict)
  hold at `auto`; Jobs returns `unrecoverable` for any external effect. Strongest thing
  in the codebase.
- **Self-Model grounding.** Verified: 0 attempts → `no_track_record`; 4 attempts (below
  the minimum) → still no ratio; 7 → a real `successRate`. It refuses to estimate.
- **The confirm gate, in the normal path.** Verified: mint → same-turn redeem refused →
  later-turn redeem succeeds.
- **Database table ownership.** One store per subsystem, no cross-subsystem raw SQL.
- **Zero import cycles at file level.**
- **`artifacts/artifact-store.js`** — id-as-filename, path re-validation on every
  resolve, metadata separated from bytes, honest tri-state `verified`.
- **`GET /api/artifacts/:id`'s security posture** — unconditional `attachment`,
  `nosniff`, sandboxing CSP, CRLF-stripped filename. Correct precisely because artifact
  content is model-authored.
- **`probe.js`'s `steps[]` narration**, `error-kind.js`, `models/router.js`'s single
  exclusion function, `MODEL_PATCH_KEYS`, the connection/model `secretRef` split.
- **The disclosure habit.** `writers/pptx.js:6-22` and `restricted-backend.js:5-13` are
  unusually honest about their own limits, and that honesty is what made this audit
  possible. The failure mode to fix is not the honesty — it is the three places where a
  claim is stated as fact and is not true (A3, "verified by round-trip", and
  `run_code`'s "no file access beyond what you've allowed").

---

## D. Corrections to claims made during this audit

Recorded because an audit that hides its own errors is not evidence.

- **`activateConversation` not clearing sticky state** is unbounded memory growth, NOT
  cross-conversation leakage — those maps are keyed by conversation id, so a
  switched-to conversation still starts clean.
- **`pipeline.js` is not an unguarded bypass.** It deliberately confirms once up front
  and has its own 30s per-step timeout.
- **The scheduling substrate is not seven independent schedulers.** The durable
  per-item scheduler exists and `ops` genuinely plugs into it; only the trivial
  `setInterval` bootstrap is duplicated.
- **Three "policy floor breached" alarms during this audit were my own test errors** —
  a count passed where an array was expected, a wrong field name, a missing required
  argument. Each produced a plausible false alarm that evaporated on reading the source.
  These policies have implicit contracts with no type signatures and no tests to catch a
  caller getting them wrong; typed Python would catch all three statically.
