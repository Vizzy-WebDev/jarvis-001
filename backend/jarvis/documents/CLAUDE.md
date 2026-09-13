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
archives full of XML, so Python's own `zipfile` plus `xml.etree.ElementTree` (a
small tree-walking parser, not a spec implementation) is all it takes.

**Two readers on purpose, both in this directory, neither split further by
format** — a real structural difference from the Node original, which had one file
per format (`docx.js`/`xlsx.js`/`pptx.js`) plus separate `media.js`/chart-data
helpers. Here:
- **`office.py`** — the richer reader everything else uses: `docx_to_markdown()`,
  `xlsx_to_markdown()`/`sheet_names()`/`sheet_rows()`, `pptx_to_markdown()`, and
  the shared embedded-picture helper `_images()`, all in one file, unified behind
  `extract_document()` — the one seam anything outside this folder should import
  (re-exported from `__init__.py`, along with each of the above). Output is
  Markdown text, so it rides through `attachments.py`'s existing message
  composition exactly like a plain text document — every model can read an Office
  document, no capability gate.
- **`reader.py`** — a second, deliberately minimal, independently-written reader
  (`read_docx()`, `read_xlsx()`, `read_document()`). Its only job is being the
  thing the artifact WRITERS (`artifacts/office.py`'s `write_docx()`/`write_xlsx()`)
  are verified against — round-tripping a written file back through the same
  reader that wrote it proves nothing, so this file exists specifically to be a
  second, unrelated implementation.

`office.py`'s `xlsx_to_markdown()` places every cell by its own `r=` reference
rather than by iteration order (a real workbook's blank cells are usually absent
from the XML entirely — walking in document order would silently shift every
later column left) and detects date-serial numbers via `styles.xml` rather than
showing a raw number like `41640`. A workbook too big to inline gets a truncation
note pointing at `jarvis/tools/analyze_spreadsheet.py`, which renders the FULL
sheet to CSV and runs a model-written script against it in `jarvis/sandbox/` —
the real ingest-once/compute-properly answer to a big spreadsheet, not more
truncation.

## What did not carry over from the Node original — disclosed, not silently missing

The Node build's equivalent had two pieces of behaviour this port does not
reproduce. Both are real, open gaps rather than docs to fix quietly:

- **No embedded-chart extraction at all.** The Node original read Office chart
  XML (`<c:strCache>`/`<c:numCache>` cache elements) into a real data table
  appended to the Markdown. `office.py` has no equivalent — a chart embedded in
  a `.docx`/`.xlsx`/`.pptx` is silently invisible to `extract_document()` today,
  never surfaced as an error, just absent from the output.
- **The embedded-picture filter is a plain byte-size cap, not a pixel-dimension
  check.** `office.py`'s `_images()` drops anything over `IMAGE_MAX_BYTES` (2MB,
  an upper bound against bloating the response) and otherwise keeps every
  image regardless of size — genuinely simpler than, and not vulnerable to, the
  specific Node bug this replaced (a LOWER-bound byte cutoff meant to filter out
  bullets/icons that also dropped small-but-legitimate JPEG photos under 4KB).
  Worth knowing if this is ever "upgraded": the Node fix for that bug
  (`imageDimensions()`, reading each format's real header bytes to filter by
  physical size instead of file size) has no Python counterpart, because the
  simpler upper-bound-only design here never had that failure mode to fix.
