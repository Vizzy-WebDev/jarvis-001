# Documents (`jarvis/documents/`)

Reads `.docx`/`.xlsx`/`.pptx` into Markdown with no new dependency: they're ZIP
archives full of XML, so Python's own `zipfile` plus `xml.etree.ElementTree` (a
small tree-walking parser, not a spec implementation) is all it takes.

**Two readers on purpose, both in this directory, neither split further by format.**
- **`office.py`** — the richer reader everything else uses: `docx_to_markdown()`,
  `xlsx_to_markdown()`/`sheet_names()`/`sheet_rows()`, `pptx_to_markdown()`, and
  the shared embedded-picture helper `_images()`, all in one file, unified behind
  `extract_document()` — the one seam anything outside this folder should import
  (re-exported from `__init__.py`, along with each of the above). Output is
  Markdown text, so it rides through `attachments.py`'s existing message
  composition exactly like a plain text document — every model can read an Office
  document, no capability gate.
- **`reader.py`** — a second, deliberately minimal, independently-written reader
  (`read_docx()`, `read_xlsx()`, `read_pptx()`, `read_document()`). Its only job is
  being the thing the artifact WRITERS (`artifacts/office.py`'s `write_docx()`/
  `write_xlsx()`/`write_pptx()`) are verified against — round-tripping a written file
  back through the same reader that wrote it proves nothing, so this file exists
  specifically to be a second, unrelated implementation. **`read_pptx()` does more
  than confirm a slide's own XML parses, on purpose**: it walks
  `presentation.xml`'s declared master and slide relationships, then the master's
  OWN relationships to its layout and theme, raising the moment any hop points at
  something missing or unparseable — a slide's text looking fine is no evidence the
  deck as a whole will open cleanly. `read_docx()`/`read_xlsx()` don't need this: a `.docx`/`.xlsx` has one
  content part, not a chain of parts that can independently disagree.

`office.py`'s `xlsx_to_markdown()` places every cell by its own `r=` reference
rather than by iteration order (a real workbook's blank cells are usually absent
from the XML entirely — walking in document order would silently shift every
later column left) and detects date-serial numbers via `styles.xml` rather than
showing a raw number like `41640`. A workbook too big to inline gets a truncation
note pointing at `jarvis/tools/analyze_spreadsheet.py`, which renders the FULL
sheet to CSV and runs a model-written script against it in `jarvis/sandbox/` —
the real ingest-once/compute-properly answer to a big spreadsheet, not more
truncation.

## Limitations

- **No embedded-chart extraction.** A chart embedded in a `.docx`/`.xlsx`/`.pptx` is
  invisible to `extract_document()` — never surfaced as an error, just absent from the
  output.
- **The embedded-picture filter is a plain upper byte-size cap** (`IMAGE_MAX_BYTES`, 2MB),
  not a pixel-dimension check. Every image under the cap is kept regardless of size.
