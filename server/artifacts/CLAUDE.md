# Output/Artifact generation — `server/artifacts/*.js`

See the root `CLAUDE.md`'s "Operational Awareness" section for the decisions that
matter beyond this file (item 3 — Output/Artifact generation — and its relationship to
item 4, Verification). This file is the module-by-module breakdown.

## `artifact-store.js`

Mirrors `uploads.js`'s own proven design deliberately — the id IS the sanitized
filename (collision-proof via a timestamp+random prefix), no in-memory index to keep in
sync, resolvable across a restart. The real difference from `uploads.js`: a generated
artifact needs real structured metadata (mime type, size, which session made it,
whether it was ever mechanically verified), which lives in the `artifacts` table
(db.js migration 19) alongside the file on disk under `data/artifacts/`.
`recordVerification(id, {verified, detail})` is written by whichever tool actually ran
the check (`create_artifact.js`, `run_code.js`) — this file never runs a check itself,
only stores the outcome. `deleteArtifact()` is the one path a mechanically-FAILED
artifact takes: never left behind pretending to be a real deliverable.

## `writers/`

Format-agnostic BY CONSTRUCTION — `create_artifact.js`'s `name` argument's own
extension decides everything; most formats need no "writer" at all (the given content
is just written as bytes). Only `.docx`/`.xlsx` need real assembly:

- **`office-zip.js`** — the shared ZIP-building helper both writers use. **Read this
  file's own header comment before touching either writer** — it documents a real,
  live-caught bug this build's own verification found: PowerShell's `Compress-Archive`
  (and even .NET's `[ZipFile]::CreateFromDirectory()`, when the entry name is derived
  from directory traversal) stores every ZIP entry path with Windows BACKSLASHES
  (`word\document.xml`), which is silently invalid per the Open Packaging Conventions
  spec real Office requires (forward slashes) — confirmed to make a freshly-written
  `.docx` fail `documents/docx.js`'s own reader outright. The fix: build each ZIP entry
  via `ZipFile.Open()` + `ZipFileExtensions.CreateEntryFromFile(zip, sourcePath,
  entryName)` with an EXPLICIT, hand-constructed forward-slash `entryName` — never
  derived from a filesystem path, so the OS's path-separator convention never leaks in.
- **`docx.js`** / **`xlsx.js`** — minimal, real, valid single-format writers (plain
  paragraphs for docx; a single sheet, `inlineStr` cells — no shared-strings table — for
  xlsx). Deliberately NOT attempting to mirror `documents/`'s full READ-side feature set
  (headings, bold, tables, images, formulas) — these are writers for Jarvis's OWN
  generated text output, not a general document-authoring engine. **Both verified by
  round-tripping their own output through this project's REAL, independently-built
  readers** (`documents/docx.js`'s `docxToMarkdown()`, `documents/xlsx.js`'s
  `readXlsx()`) — the honest verification technique available in an environment with no
  real Word/Excel to open a file in, and the exact technique that caught the
  backslash-path bug above before it ever shipped.
- **`pptx.js` — built, but carries a genuinely different, narrower verification
  confidence than `docx.js`/`xlsx.js`, disclosed rather than glossed over.** A real
  PowerPoint deck needs more required parts than docx/xlsx do — `ppt/presentation.xml`,
  a real slideMaster + slideLayout + theme chain, and one `slideN.xml` per slide, each
  with its own `.rels`. `documents/pptx.js`'s own reader never opens the master/layout/
  theme parts at all (it only reads `ppt/presentation.xml`'s slide order and each
  slide's own shape/text) — meaning the round-trip technique that already caught the
  ZIP path-separator bug for docx/xlsx **cannot** validate that chain the same way. The
  master/layout/theme XML in `pptx.js` is written from real OOXML DrawingML/
  PresentationML schema knowledge, in good faith, but **has not been opened in real
  PowerPoint as of this build** — the same honest-gap disclosure `sandbox/CLAUDE.md`
  already carries for `wsl-backend.js` ("don't trust it... until a real run confirms
  it"). What WAS verified here: real ZIP validity (a genuine `PK` signature, every one
  of 15 entries forward-slash — the same fix carried over correctly), and a full
  round-trip through `documents/pptx.js`'s real reader confirming slide order and
  title/body text are structurally exactly right. Open a generated `.pptx` in real
  PowerPoint before trusting the master/theme chain the way docx/xlsx are trusted.

## `create_artifact.js` (`server/tools/`) — the write path

`core:true, meta:true`, **no confirm gate of its own** — the "hybrid creation model"
per the owner's own explicit choice (an explicit request creates directly; an
unprompted Jarvis-initiated proposal waits for a yes first) lives entirely in
`prompt.js`'s own instruction text, not a token-gated mechanism, since it's a
conversational judgment call the same way `remember_about_me`'s own approval flow is.

**Every artifact is mechanically verified the instant it's created, and a failure is
never left on disk pretending to be real** — `ops/verify.js`'s `verifyFileOpens()` runs
immediately after `saveArtifact()`; a failure calls `deleteArtifact()` and returns a
real error to the model in the SAME turn. Deliberately NO retry here (unlike Jobs' own
retry-then-escalate) — these writers are deterministic, so a mechanical failure is a
real bug that would fail identically on a retry, not a transient issue worth spending a
retry budget on. A pass calls `recordVerification(id, {verified:true})` — **a real bug
this build's own testing caught**: the first version checked-but-never-recorded the
success case, leaving every kept artifact's `verified` column permanently `null` even
though a real check had already run and passed; fixed, re-verified.

**One honest capability ceiling, stated in the tool's own description, not silently
omitted**: raster/photographic image generation is not possible — none of the three
model adapters does image generation, and no image library exists in this project's
five-dependency budget. Diagrams/charts ARE real (genuine SVG vector markup).

## `run_code.js`'s own artifact integration (`server/sandbox/`, `server/tools/`)

`sandbox/restricted-backend.js` (the one actually trusted/verified sandbox backend —
see `server/sandbox/CLAUDE.md` on why `wsl-backend.js` isn't touched here) now snapshots
which filenames were given as INPUT before a run, and — before its own `finally` block
deletes the throwaway temp folder — reads back any file present that WASN'T part of the
input as real `outputFiles` (binary-safe Buffers, capped at 20 files). `run_code.js`
turns each into a real artifact through the exact same `saveArtifact` ->
`verifyFileOpens` -> `recordVerification`-or-`deleteArtifact` path `create_artifact.js`
uses — one honest, single verification discipline, never a second one. A single
generated file gets a real file card in the transcript (the same `ui_action` shape
`create_artifact.js` uses); a rarer multi-file run still lists every artifact in the
tool result (visible to the model to mention) without a dedicated card for each — a
disclosed scope limit, not a bug.

## Serving (`server.js`)

`GET /api/artifacts` (list) and `GET /api/artifacts/:id` (the file itself) — same shape
as the existing screenshots routes (`screenshotPath()`): a store module owns path
validation (`artifactFilePath()`), the route just serves the file. **Verified against a
real running server, not just direct function calls** — a real `create_artifact` call,
then a real `curl` against the live HTTP route, confirmed the correct `Content-Type`
header and byte-for-byte matching content (a real ZIP signature, exact size match).

**SECURITY (found by a background review, fixed same-session): `Content-Disposition:
attachment` is UNCONDITIONAL, not gated behind a `?download=1` param the way an
earlier version had it.** An artifact's content comes from a model (`create_artifact.js`
places no restriction on what an `.svg`/`.html`-extensioned file's text content
contains, and SVG genuinely executes an embedded `<script>` when rendered inline by a
browser) — a plain, un-parameterized GET (exactly what a link, an `<iframe>`, or a
manually typed URL produces) used to render the file INLINE, in this app's own origin, a
real stored-XSS path. Fixed: attachment disposition always forces a real download now,
regardless of query params, plus `X-Content-Type-Options: nosniff` and a sandboxing CSP
as defense in depth. `record.name` is stripped of `\r`/`\n` before landing in the
`Content-Disposition` header value — an unsanitized filename carrying a raw newline
could otherwise inject additional response headers.

## Front-end (`public/app.js`)

`addArtifactCard()` — a new function reusing the same `.doc-card`/`.doc-card-head` CSS
classes `addDocumentCard()` already established, but its own function rather than one
more special case bolted onto that one (a file card's shape — name, mime type, size, a
real download link — has nothing in common with a document card's markdown body/
sources/copy-button). Wired via the same `ui_action` dispatch pattern every other
tool-driven UI effect in this file already uses (`navigate`, `memory_review`,
`attachment`, ...) — `type:'artifact_created'`. **Built following the established
pattern, not live-browser-tested** — the file card's actual visual rendering needs the
owner's own browser check, the same disclosed-gap discipline this project applies to
every front-end change that couldn't be verified via `agent-browser` in the session that
wrote it.
