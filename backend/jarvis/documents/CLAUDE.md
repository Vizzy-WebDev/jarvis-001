<!-- Ported from the Node build during the S6 cutover. The architecture, the
invariants and the live-caught bugs described here all carried over deliberately and
still hold. File paths have been updated to their real Python counterparts and are
verified to exist. Function names written in camelCase (`getToolDeclarations()`) are
the NODE originals, kept because the surrounding reasoning is about them; the Python
equivalent is the snake_case function doing that job in the same module. Where a Node
module had no Python counterpart, the text says so rather than pointing at a file that
does not exist. -->

# Documents (`jarvis/documents/`)

Reads `.docx`/`.xlsx`/`.pptx` into Markdown with no new dependency: they're ZIP
archives full of XML, so `artifacts/office.py` (a minimal reader over Node's built-in
`zlib.inflateRawSync`) plus `artifacts/office.py` (a small tree-building parser, not a spec
implementation) is all it takes. One reader per format (`docx.py`/`xlsx.py`/
`pptx.py`) plus two shared pieces (`media.py` for embedded pictures, `artifacts/office.py`
for chart data), unified behind `__init__.py`'s `extractDocument()` — the only seam
anything outside this folder should import. Output is Markdown text, so it rides
through `attachments.py`'s existing `composeMessage()` exactly like a plain text
document — every model can read an Office document, not just Gemini, no
capability gate.

`xlsx.py` places every cell by its own `r=` reference rather than by iteration
order (a real workbook's blank cells are usually absent from the XML entirely —
walking in document order would silently shift every later column left) and
detects date-serial numbers via `styles.xml` rather than showing a raw number
like `41640`. A workbook too big to inline gets a truncation note pointing at
`jarvis/tools/analyze_spreadsheet.py`, which renders the FULL sheet to CSV and
runs a model-written script against it in `jarvis/sandbox/` — the real
ingest-once/compute-properly answer to a big spreadsheet, not more truncation.

## Gotchas

- **`media.py`'s embedded-picture extractor filters by real pixel dimensions,
  not file size.** A byte-size cutoff ("drop anything under 8KB, it's probably
  a bullet or an icon") silently dropped genuine small-but-legitimate photos —
  ordinary JPEG compression can put a real photo under 4KB. `imageDimensions()`
  reads each format's actual header bytes (fixed offsets for PNG/GIF/BMP, a
  marker-segment walk for JPEG) and filters on physical size instead — file
  size conflates visual content with compression efficiency, which are
  unrelated.
- **`artifacts/office.py` must look up cache elements recursively, not as direct
  children.** A `<c:strCache>`/`<c:numCache>` in Office chart XML is never a
  direct child of `<c:tx>`/`<c:cat>`/`<c:val>` — it's one level deeper, wrapped
  in a `<c:strRef>`/`<c:numRef>`. A direct-children-only lookup silently
  produces an empty table (headers, no rows) for every chart, with no error —
  this class of bug only shows up by rendering real output, never by static
  review. Fixed with a recursive lookup (`findAll` instead of `findChild`).
  Chart-position-within-the-document is a separate, deliberately unsolved
  problem — see `artifacts/office.py`'s own header comment for why every chart in an
  archive is appended as one section rather than interleaved at its real
  location.
