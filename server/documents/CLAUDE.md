# Documents (`server/documents/`)

Reads `.docx`/`.xlsx`/`.pptx` into Markdown with no new dependency: they're ZIP
archives full of XML, so `zip.js` (a minimal reader over Node's built-in
`zlib.inflateRawSync`) plus `xml.js` (a small tree-building parser, not a spec
implementation) is all it takes. One reader per format (`docx.js`/`xlsx.js`/
`pptx.js`) plus two shared pieces (`media.js` for embedded pictures, `charts.js`
for chart data), unified behind `index.js`'s `extractDocument()` — the only seam
anything outside this folder should import. Output is Markdown text, so it rides
through `attachments.js`'s existing `composeMessage()` exactly like a plain text
document — every model can read an Office document, not just Gemini, no
capability gate.

`xlsx.js` places every cell by its own `r=` reference rather than by iteration
order (a real workbook's blank cells are usually absent from the XML entirely —
walking in document order would silently shift every later column left) and
detects date-serial numbers via `styles.xml` rather than showing a raw number
like `41640`. A workbook too big to inline gets a truncation note pointing at
`server/skills/analyze_spreadsheet.js`, which renders the FULL sheet to CSV and
runs a model-written script against it in `server/sandbox/` — the real
ingest-once/compute-properly answer to a big spreadsheet, not more truncation.

## Gotchas

- **`media.js`'s embedded-picture extractor filters by real pixel dimensions,
  not file size.** A byte-size cutoff ("drop anything under 8KB, it's probably
  a bullet or an icon") silently dropped genuine small-but-legitimate photos —
  ordinary JPEG compression can put a real photo under 4KB. `imageDimensions()`
  reads each format's actual header bytes (fixed offsets for PNG/GIF/BMP, a
  marker-segment walk for JPEG) and filters on physical size instead — file
  size conflates visual content with compression efficiency, which are
  unrelated.
- **`charts.js` must look up cache elements recursively, not as direct
  children.** A `<c:strCache>`/`<c:numCache>` in Office chart XML is never a
  direct child of `<c:tx>`/`<c:cat>`/`<c:val>` — it's one level deeper, wrapped
  in a `<c:strRef>`/`<c:numRef>`. A direct-children-only lookup silently
  produces an empty table (headers, no rows) for every chart, with no error —
  this class of bug only shows up by rendering real output, never by static
  review. Fixed with a recursive lookup (`findAll` instead of `findChild`).
  Chart-position-within-the-document is a separate, deliberately unsolved
  problem — see `charts.js`'s own header comment for why every chart in an
  archive is appended as one section rather than interleaved at its real
  location.
