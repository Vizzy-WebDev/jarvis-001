# Output/Artifact generation — `jarvis/artifacts/`

See the root `CLAUDE.md`'s "Operational Awareness" section for the decisions that matter
beyond this file (Output/Artifact generation and its relationship to Verification). This is
the module-by-module breakdown.

## `store.py`

The id IS the file's on-disk name (`art_<random>`, so it cannot collide and needs no
in-memory index). The user sees `Artifact.name`; the filesystem holds the id, so the two
cannot be made to disagree in a way that escapes `data/artifacts/`. Structured metadata
(mime type, size, session, whether it was mechanically verified) lives in the `artifacts`
table.

`keep(source, name, session_id)` is the one write path: it **verifies first**, then keeps or
deletes. A file that fails to re-open is deleted immediately and a `ValueError` is raised,
so a broken file is never left behind pretending to be a deliverable. `verify()` returns
`(True|False|None, why)`; `None` means "not checkable" (anything outside `.docx`/`.xlsx`/
`.pptx`) and is never recorded as verified. Also: `get()`, `recent()`, `safe_name()`,
`mime_for()`.

## `office.py`

Minimal, real writers: `write_docx` (a paragraph list), `write_xlsx` (one sheet of
`inlineStr` cells, no shared-strings table) and `write_pptx` (title-and-bullets slides on
one fixed layout, one theme, widescreen, constant master/layout/theme boilerplate). They are
deliberately not layout libraries, and accept no styling argument that would silently do
nothing.

**Every ZIP entry name is a hand-built forward-slash string**, never derived from a
filesystem path. Office formats follow the Open Packaging Conventions, which require forward
slashes; a name picked up from walking a directory carries the OS separator, and on Windows
that yields a file that looks fine, has the right signature, and will not open in Word.
`entry_names()` reads the stored names back so a test checks a real generated file rather
than trusting it.

**Writers are verified by round-tripping through an independent reader**,
`documents/reader.py` (`read_docx`/`read_xlsx`/`read_pptx`) — reading a file back through the
code that wrote it proves nothing. `read_pptx()` walks the whole master/layout/theme chain
and raises on a broken hop, so it checks the deck as a whole and not just one slide's XML.

## `tools/create_artifact.py` — the write path

Not `meta` (a background job strips meta tools, so a job asked to produce a report could not
produce one) and no confirm gate of its own: an explicit request creates directly, while an
unprompted proposal waits for a yes — that judgment lives in `prompt.py`'s instructions. The
file extension in `filename` decides everything: `.docx` / `.xlsx` / `.pptx` go through
their writers (with plain-text fallbacks: blank-line-separated paragraphs, CSV, and
blank-line-separated slides), and `.txt .md .csv .json .html .svg` are written exactly as
given. `keep()` verifies on the spot. There is deliberately NO retry on a failed check: the
writers are deterministic, so a failure is a real bug that would fail identically again.

**One capability ceiling, stated in the tool's own description:** no raster/photographic
image generation exists (no image library is a dependency). SVG
diagrams and charts are real vector markup.

## `tools/run_code.py`

Files a sandboxed script produces (`sandbox/runner.py`'s `SandboxResult.files`, up to
`MAX_OUTPUT_FILES`) each go through the same `keep()` path, so there is one verification
discipline, not two. A produced file that fails verification is reported in the result with
its error rather than dropped, because "it made a file" and "it made a file that opens" are
different claims.

## Serving (`routes/artifacts.py`)

`GET /api/artifacts` lists; `GET /api/artifacts/:id` serves the file.

**Security: `Content-Disposition: attachment` is UNCONDITIONAL, never behind a query
parameter.** An artifact's content comes from a model, and an `.svg` or `.html` executes an
embedded `<script>` when rendered inline, so an inline render was a stored-XSS path in the
app's own origin. The route also sets `X-Content-Type-Options: nosniff` and a sandboxing CSP
as defence in depth, and strips CR/LF from the filename before it goes into a header value,
since an unsanitised name could inject response headers. Re-confirm this against a live
request whenever the route changes.
