# Artifacts — `jarvis/artifacts/`

Real files Jarvis makes for the person: made when they ask, shown in the chat that asked,
kept on the Artifacts page, opened back in that chat, deleted by them. The format is open —
Word, Excel, PowerPoint, PDF, and any text format (Markdown, HTML, SVG, CSV, JSON, code in
any language). Rendering is the front end's job (`frontend/components/artifacts/`); this
package stores, verifies and describes.

## The behaviour (decided with the person who owns this app)

- **Asked for a file → made at once, no confirmation step.** `create_artifact` is `Risk.LOW`:
  the file lands only in `data/artifacts/`, nothing outside changes, and the person can
  delete it. It is tagged `core`, so it is declared on every turn — behind `find_capability`
  a keyword search had to match, and "an HTML page", "a CSV", "a diagram" all missed.
- **Not asked, but clearly useful → offer in one sentence, make nothing until "yes".**
  That judgement is `prompt.py`'s `MAKING_ARTIFACTS` block (Jarvis's stable instruction, and
  a specialist's when the person talks to it directly). It is about the conversation, not
  about the call's safety, so it is not a policy gate.
- **Otherwise → just answer.**

## `store.py`

The id IS the file's on-disk name (`art_<random>`); the user sees `name`/`title`. The path is
always derived from the id, never from a name, so nothing a model writes can steer where a
file lands.

- `keep(source, *, name, session_id, conversation_id, title)` — the ONE write path. It
  **verifies first**: a `.docx/.xlsx/.pptx/.pdf` is re-opened with an independently written
  reader (`documents/reader.py`), and one that fails is deleted and `ValueError` raised, never
  recorded. Anything else is `verified=None` ("not checkable"), never `True`. It removes the
  staging folder afterwards (`staging_path()` / `discard_staging()`).
- **The conversation link.** `session_id` is the session the making turn ran in;
  `conversation_id` is the chat Open in Chat goes back to. They differ on purpose: a
  specialist runs under `agent:<id>:<conversation>`, a background job under its own id.
  `conversation_for(session_id)` resolves it once, at creation — a real `conversations` row or
  nothing. Tools pass `ctx.session_id` (`wants_context=True`); **a tool that saves a file
  without it produces an artifact no chat can find** — that was the original bug.
- `list_page(limit, before, q, kind)` pages newest-first by a `<created_at>|<id>` cursor (a
  millisecond tie cannot drop or repeat a row). `kind_for(name)` is the one category table
  (document, spreadsheet, presentation, pdf, markdown, web, image, audio, data, code, text,
  other) the page filters by and the viewer switches on. `conversations_of(artifacts)` gives
  each chat's title and state (`live` / `trashed` / `gone`) in one query.
- `delete(id)` removes file then row. Only the person deletes — no capability calls it (the
  same rule as Content Management).

## Writers — `office.py`, `pdf.py`

`write_docx` / `write_xlsx` / `write_pptx`: minimal, real writers. **Every ZIP entry name is a
hand-built forward-slash string**, never from a filesystem path — Office requires forward
slashes, and a Windows-walked name yields a file that will not open in Word. `entry_names()`
reads them back for tests.

`write_pdf`: a dependency-free text PDF — headings, paragraphs, bullets, code blocks, wrapped
and paginated on A4, in the standard fonts (nothing embedded). Those fonts carry only
Windows-1252, so a character outside it shows as "?" and the count is returned; the tool
reports it as a `warning` rather than handing over a PDF that quietly lost text. Verified by
`documents/reader.py::read_pdf` (cross-reference offsets land on their objects, pages exist,
text reads back).

## `tools/create_artifact.py`

The extension decides: `.docx/.xlsx/.pptx/.pdf` through their writers (with plain-text
fallbacks for `content`), anything else written exactly as given. Refused, in plain words:
Windows programs and scripts a double-click would run (`RUNNABLE`), raster images (there is
no image generation — SVG is the honest alternative), media (→ `narrate_to_file`), and binary
formats this cannot produce truthfully. No retry on a failed check: the writers are
deterministic. The result's `ui_action` (`attachment_action()`) carries `artifactId`, `title`,
`artifactKind` and `size` so the chat card can open it — `run_code` and `narrate_to_file` use
the same shape. Not `meta`: a background job must be able to make a report.

## Serving and viewing (`routes/artifacts.py`)

`GET /api/artifacts` (paged, `q`, `kind`; `nextBefore` only when there is more, so the
recorded empty response is unchanged), `GET /:id/info`, `GET /:id/preview` (Word/PowerPoint
as Markdown text, Excel as rows — JSON, never the file), `GET /:id` (the file), `DELETE /:id`.

**The file route's `Content-Disposition: attachment` is UNCONDITIONAL** (plus `nosniff`, a
sandboxing CSP, CR/LF stripped from the filename). An inline `.svg`/`.html` in the app's own
origin was a stored-XSS path. Re-confirm against a live request whenever the route changes.

**Viewing never goes through an inline route.** The front end fetches the bytes and renders
them itself: text as text nodes, images (SVG included) through `<img>`, a web page in
`<iframe sandbox="allow-scripts">` (opaque origin) with a strict CSP placed first in its
document. What a sealed page can still do — navigate its own frame to a URL — is closed by
`jarvis/request_guard.py`, which refuses any `/api` request a browser labels `Origin: null`
or `Sec-Fetch-Site: cross-site`. **Do not add `allow-same-origin` to that frame, and do not
remove the guard**: together they are what makes running a model-written page safe.
