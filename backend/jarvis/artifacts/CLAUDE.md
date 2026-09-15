<!-- Ported from the Node build during the S6 cutover. The architecture, the
invariants and the live-caught bugs described here all carried over deliberately and
still hold. File paths have been updated to their real Python counterparts and are
verified to exist. Function names written in camelCase (`getToolDeclarations()`) are
the NODE originals, kept because the surrounding reasoning is about them; the Python
equivalent is the snake_case function doing that job in the same module. Where a Node
module had no Python counterpart, the text says so rather than pointing at a file that
does not exist. -->

# Output/Artifact generation — `jarvis/artifacts/*.py`

See the root `CLAUDE.md`'s "Operational Awareness" section for the decisions that
matter beyond this file (item 3 — Output/Artifact generation — and its relationship to
item 4, Verification). This file is the module-by-module breakdown.

## `artifacts/store.py`

Mirrors `uploads.py`'s own proven design deliberately — the id IS the sanitized
filename (collision-proof via a timestamp+random prefix), no in-memory index to keep in
sync, resolvable across a restart. The real difference from `uploads.py`: a generated
artifact needs real structured metadata (mime type, size, which session made it,
whether it was ever mechanically verified), which lives in the `artifacts` table
(db.py migration 19) alongside the file on disk under `data/artifacts/`.
`recordVerification(id, {verified, detail})` is written by whichever tool actually ran
the check (`create_artifact.py`, `run_code.py`) — this file never runs a check itself,
only stores the outcome. `deleteArtifact()` is the one path a mechanically-FAILED
artifact takes: never left behind pretending to be a real deliverable.

## `artifacts/office.py`

Format-agnostic BY CONSTRUCTION — `create_artifact.py`'s `name` argument's own
extension decides everything; most formats need no "writer" at all (the given content
is just written as bytes). `.docx`/`.xlsx`/`.pptx` need real assembly, and all three
writers (`write_docx`, `write_xlsx`, `write_pptx`) live together in this one file, not
a `writers/` subdirectory and not one file per format the way the Node original split
them — there was never enough writer-specific logic here to justify the split.

- **This file's own header comment documents a real, live-caught bug** this build's
  own verification found: PowerShell's `Compress-Archive` (and even .NET's
  `[ZipFile]::CreateFromDirectory()`, when the entry name is derived from directory
  traversal) stores every ZIP entry path with Windows BACKSLASHES
  (`word\document.xml`), which is silently invalid per the Open Packaging Conventions
  spec real Office requires (forward slashes) — confirmed to make a freshly-written
  `.docx` fail `documents/office.py`'s own reader outright. The fix: build each ZIP
  entry with an EXPLICIT, hand-constructed forward-slash entry name — never derived
  from a filesystem path, so the OS's path-separator convention never leaks in.
- **`write_docx` / `write_xlsx`** — minimal, real, valid single-format writers (plain
  paragraphs for docx; a single sheet, `inlineStr` cells — no shared-strings table — for
  xlsx). Deliberately NOT attempting to mirror `documents/`'s full READ-side feature set
  (headings, bold, tables, images, formulas) — these are writers for Jarvis's OWN
  generated text output, not a general document-authoring engine. **Both verified by
  round-tripping their own output through this project's REAL, independently-built
  reader** (`documents/reader.py`'s `read_docx()`/`read_xlsx()`) — the honest
  verification technique available in an environment with no real Word/Excel to open a
  file in, and the exact technique that caught the backslash-path bug above before it
  ever shipped.
- **`write_pptx`** — a real OOXML PresentationML writer, closing what used to be a
  disclosed gap ("PowerPoint can be read but never written"). One fixed slide layout
  (title + body text, plain paragraphs — no real bulleted-list glyphs, same honesty
  `write_docx` already practices by never emitting `<w:numPr>` list numbering either),
  one fixed theme, widescreen. A real presentation needs more parts than a `.docx`/
  `.xlsx` ever did — `[Content_Types].xml`, `_rels/.rels`, `ppt/presentation.xml` (+
  its own `_rels`), a slideMaster/slideLayout/theme chain, and one `ppt/slides/slideN.xml`
  (+ its own `_rels`) per slide — but the master/layout/theme are CONSTANT boilerplate
  every deck, the same way `write_docx`'s own `[Content_Types].xml` never changes
  either; only `presentation.xml`, its rels, and the per-slide parts vary with content.
  No `docProps/` — `write_docx`/`write_xlsx` already omit it and open fine, so this
  stays consistent rather than starting a new convention. **Verified by round-tripping
  through `documents/reader.py`'s own `read_pptx()`** — see that file's own header for
  why this one does more than confirm a slide's XML parses: an earlier build's own
  pptx writer disclosed a real master/theme verification gap (a slide's text checking
  out was never proof the master it depends on actually resolved to a real layout),
  and `read_pptx()` walks that whole chain specifically so the same gap can't recur
  silently here. **A related, pre-existing bug found and fixed while building this**:
  `documents/office.py`'s own `R` namespace constant (used to resolve an `r:id`
  attribute like `<p:sldId r:id="...">`) was set to a `.rels` FILE's own root
  namespace, not the namespace an `r:id` attribute actually lives in — silently
  masked for xlsx/pptx reading by a positional/sorted fallback that happened to still
  land on the right part for a well-formed, sequentially-named file. `write_pptx`'s
  own verification reader has no such fallback (a broken chain must raise, not
  silently resolve to a plausible guess), which is what surfaced it. Fixed in both
  `documents/office.py` and the new `documents/reader.py` constant.

## `create_artifact.py` (`jarvis/tools/`) — the write path

`core:true, meta:true`, **no confirm gate of its own** — the "hybrid creation model"
per the owner's own explicit choice (an explicit request creates directly; an
unprompted Jarvis-initiated proposal waits for a yes first) lives entirely in
`prompt.py`'s own instruction text, not a token-gated mechanism, since it's a
conversational judgment call the same way `remember_about_me`'s own approval flow is.

**Every artifact is mechanically verified the instant it's created, and a failure is
never left on disk pretending to be real** — `ops/verify.py`'s `verifyFileOpens()` runs
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

## `run_code.py`'s own artifact integration (`jarvis/sandbox/`, `jarvis/tools/`)

`sandbox/runner.py` (the one actually trusted/verified sandbox backend —
see `jarvis/sandbox/CLAUDE.md` on why `sandbox/runner.py` isn't touched here) now snapshots
which filenames were given as INPUT before a run, and — before its own `finally` block
deletes the throwaway temp folder — reads back any file present that WASN'T part of the
input as real `outputFiles` (binary-safe Buffers, capped at 20 files). `run_code.py`
turns each into a real artifact through the exact same `saveArtifact` ->
`verifyFileOpens` -> `recordVerification`-or-`deleteArtifact` path `create_artifact.py`
uses — one honest, single verification discipline, never a second one. A single
generated file gets a real file card in the transcript (the same `ui_action` shape
`create_artifact.py` uses); a rarer multi-file run still lists every artifact in the
tool result (visible to the model to mention) without a dedicated card for each — a
disclosed scope limit, not a bug.

## Serving (`main.py`)

`GET /api/artifacts` (list) and `GET /api/artifacts/:id` (the file itself) — same shape
as the existing screenshots routes (`screenshotPath()`): a store module owns path
validation (`artifactFilePath()`), the route just serves the file. **Verified against a
real running server, not just direct function calls** — a real `create_artifact` call,
then a real `curl` against the live HTTP route, confirmed the correct `Content-Type`
header and byte-for-byte matching content (a real ZIP signature, exact size match).

**SECURITY (found by a background review, fixed same-session): `Content-Disposition:
attachment` is UNCONDITIONAL, not gated behind a `?download=1` param the way an
earlier version had it.** An artifact's content comes from a model (`create_artifact.py`
places no restriction on what an `.svg`/`.html`-extensioned file's text content
contains, and SVG genuinely executes an embedded `<script>` when rendered inline by a
browser) — a plain, un-parameterized GET (exactly what a link, an `<iframe>`, or a
manually typed URL produces) used to render the file INLINE, in this app's own origin, a
real stored-XSS path. Fixed: attachment disposition always forces a real download now,
regardless of query params, plus `X-Content-Type-Options: nosniff` and a sandboxing CSP
as defense in depth. `record.name` is stripped of `\r`/`\n` before landing in the
`Content-Disposition` header value — an unsanitized filename carrying a raw newline
could otherwise inject additional response headers.

## Front-end (`frontend/app/page.tsx`)

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
