"""Producing a real file the user can open — an artifact.

**No confirmation, and that is a decision, not an omission.** The file lands in
Jarvis's own artifacts folder (never elsewhere on the computer), touches nothing
outside it, and the person can delete it from the Artifacts page. So it is LOW
risk: when they ASK for a file, it is made in that turn. When they did not ask,
Jarvis offers first and makes nothing until they say yes — that judgement is in
`prompt.py` (`MAKING_ARTIFACTS`), because it is about the conversation, not about
this call's safety. Contrast `write_file`, which writes to a folder on the
computer and keeps its own rules.

**Core**, so it is declared on every turn. Behind `find_capability` it was only
reachable when a keyword search happened to match the request, and "an HTML
page", "a CSV", "a diagram" and "write this up" all missed.

Deliberately NOT marked meta: a background job strips meta tools before anything
else applies, so a job asked to produce a report structurally could not produce one.

**Formats are open by design.** Word, Excel, PowerPoint and PDF go through real
writers and are re-opened before they are kept; any other extension is written
exactly as given, as text (Markdown, code in any language, HTML, SVG, CSV, JSON
…). Refused: Windows programs and scripts a person could double-click (a
model-written `.bat` is a way to run anything), raster images (there is no image
generation here — an SVG is the honest alternative), and binary formats this
cannot produce truthfully.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..artifacts import discard_staging, keep, safe_name, staging_path
from ..artifacts import write_docx, write_pptx, write_xlsx
from ..artifacts.pdf import write_pdf
from ..capabilities import CapabilitySpec, Risk

#: Double-clicking any of these on Windows runs it (or installs it, or changes the
#: system). A file a model wrote must never be one click from doing that.
RUNNABLE = frozenset({
    ".exe", ".dll", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".jse",
    ".wsf", ".wsh", ".hta", ".scr", ".pif", ".cpl", ".msi", ".msp", ".msc", ".reg", ".lnk",
    ".jar", ".appref-ms", ".application", ".gadget", ".inf", ".url", ".scf",
})
RASTER = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff",
                    ".heic", ".avif", ".psd"})
MEDIA = frozenset({".mp3", ".wav", ".ogg", ".opus", ".aac", ".flac", ".m4a", ".mp4", ".mov",
                   ".webm", ".avi", ".mkv"})
#: Binary formats with no truthful text form here; each has a format that works.
OTHER_BINARY = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx", ".odt": ".docx",
                ".ods": ".xlsx", ".odp": ".pptx", ".rtf": ".docx", ".zip": None, ".7z": None,
                ".rar": None, ".tar": None, ".gz": None, ".epub": None, ".sqlite": None,
                ".db": None, ".woff": None, ".woff2": None, ".ttf": None, ".otf": None}


def _refusal(suffix: str) -> str | None:
    if suffix in RUNNABLE:
        return (f"I don't make {suffix} files: opening one runs it on the computer. I can "
                "write the same thing as a plain text file for you to read first.")
    if suffix in RASTER:
        return ("I can't make images or photographs — there is no image generation here. I "
                "can draw a diagram or chart as an .svg file instead.")
    if suffix in MEDIA:
        return (f"I can't make a {suffix} file this way. For a spoken voice-over I can use "
                "narrate_to_file.")
    if suffix in OTHER_BINARY:
        better = OTHER_BINARY[suffix]
        return (f"I can't produce a real {suffix} file."
                + (f" I can make a {better} instead." if better else ""))
    return None


def _run(filename: str = "", content: str = "", title: str = "",
         rows: list[Any] | None = None, paragraphs: list[str] | None = None,
         slides: list[dict[str, Any]] | None = None, ctx: Any = None) -> dict[str, Any]:
    name = safe_name(filename)
    if not name or "." not in name:
        return {"ok": False, "error": "I need a filename with an extension."}
    suffix = Path(name).suffix.lower()
    refused = _refusal(suffix)
    if refused:
        return {"ok": False, "error": refused}

    staging = staging_path(name)
    lost = 0
    try:
        if suffix == ".docx":
            body = paragraphs or [line for line in (content or "").split("\n\n") if line.strip()]
            if not body:
                return _nothing(staging, "document")
            write_docx(staging, body)
        elif suffix == ".xlsx":
            grid = rows or _rows_from_csv(content)
            if not grid:
                return _nothing(staging, "spreadsheet")
            write_xlsx(staging, grid)
        elif suffix == ".pptx":
            deck = slides or _slides_from_text(content)
            if not deck:
                return _nothing(staging, "presentation")
            write_pptx(staging, deck)
        elif suffix == ".pdf":
            text = content or "\n\n".join(paragraphs or [])
            if not text.strip():
                return _nothing(staging, "PDF")
            _, lost = write_pdf(staging, text, title=title or None)
        else:
            # Written exactly as given: for these, the content IS the file, and
            # reformatting it would be changing what was asked for.
            if not (content or "").strip():
                return _nothing(staging, "file")
            staging.write_text(content, encoding="utf-8")
    except Exception as err:  # noqa: BLE001
        discard_staging(staging)
        return {"ok": False, "error": f"I couldn't build that file: {err}"}

    session_id = getattr(ctx, "session_id", None)
    try:
        artifact = keep(staging, name=name, session_id=session_id, title=title or None)
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    result = artifact.as_result()
    note = (f" {lost} character{'s' if lost != 1 else ''} couldn't be shown in the PDF's "
            "fonts and appear as '?'. A .docx keeps every character."
            if lost else "")
    # Shown in the conversation as a file to open — "it's here in the conversation"
    # has to be true on the screen, not only in the model's words.
    return {"ok": True, **result,
            **({"warning": note.strip()} if note else {}),
            "ui_action": attachment_action(result),
            "speak": f"{artifact.display_title} is ready — it's here in the conversation."}


def attachment_action(result: dict[str, Any]) -> dict[str, Any]:
    """The card a saved artifact shows as in the chat. One shape for every tool."""
    return {"type": "attachment", "kind": "audio" if result.get("kind") == "audio" else "file",
            "url": result["url"], "mimeType": result.get("mimeType") or "",
            "name": result.get("name") or "", "artifactId": result.get("id"),
            "title": result.get("title"), "artifactKind": result.get("kind"),
            "size": result.get("size")}


def _nothing(staging: Path, what: str) -> dict[str, Any]:
    discard_staging(staging)
    return {"ok": False, "error": f"There's nothing to put in the {what}."}


def _rows_from_csv(content: str) -> list[list[str]]:
    import csv
    import io

    if not (content or "").strip():
        return []
    return [row for row in csv.reader(io.StringIO(content))]


def _slides_from_text(content: str) -> list[dict[str, Any]]:
    """The same fallback-from-plain-text convention `.docx`'s blank-line split
    and `.xlsx`'s `_rows_from_csv` already use: a model that just sends
    `content` with no structured `slides` still gets a real deck. Each
    blank-line-separated block becomes one slide — its first line the title,
    the rest its bullets (a leading `-`/`*` stripped if the model wrote one)."""
    blocks = [b.strip() for b in (content or "").split("\n\n") if b.strip()]
    slides = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        title, *rest = lines
        slides.append({"title": title, "bullets": [re.sub(r"^[-*]\s*", "", line) for line in rest]})
    return slides


SPEC = CapabilitySpec(
    id="builtin.create_artifact", name="create_artifact",
    description=(
        "Save a real file (an artifact) the user can open, view and download; it appears in "
        "this chat and on their Artifacts page. Any format: a Word document (.docx), a "
        "spreadsheet (.xlsx), a presentation (.pptx), a PDF (.pdf, plain text and headings), "
        "or any text format written exactly as given — Markdown (.md), a web page or small "
        "interactive tool (.html, one self-contained file with inline CSS/JS), a diagram or "
        "chart (.svg), data (.csv, .json), code in any language (.py, .js, .sql …), plain "
        "text. Make one when the user asks for a file or something to keep; if they did not "
        "ask, offer first. Cannot make images or photographs (use .svg for diagrams), or "
        "programs a double-click would run. Different from write_file, which writes to a "
        "folder on their computer."),
    input_schema={"type": "object", "properties": {
        "filename": {"type": "string", "description": "Including the extension, e.g. "
                                                      "\"q3-report.docx\" or \"budget.csv\"."},
        "title": {"type": "string",
                  "description": "A short human title shown on the Artifacts page, e.g. "
                                 "\"Q3 sales report\"."},
        "content": {"type": "string",
                    "description": "The full text of the file. For .xlsx, CSV rows if `rows` "
                                   "is not given. For .pptx, blank-line-separated slides "
                                   "(first line of each is the title) if `slides` is not given. "
                                   "For .docx, blank-line-separated paragraphs if `paragraphs` "
                                   "is not given. For .pdf, text with optional #/##/### "
                                   "headings and - bullets."},
        # Every array says what it holds: some providers refuse one that doesn't
        # (Gemini: "items: missing field"), which fails every turn that declares it.
        # A row's cells are left untyped on purpose — text and numbers both belong there.
        "rows": {"type": "array", "items": {"type": "array"},
                 "description": "For .xlsx: a list of rows, each a list."},
        "paragraphs": {"type": "array", "items": {"type": "string"},
                       "description": "For .docx: one string per paragraph."},
        "slides": {"type": "array",
                   "items": {"type": "object", "properties": {
                       "title": {"type": "string"},
                       "bullets": {"type": "array", "items": {"type": "string"}}}},
                   "description": "For .pptx: a list of slides, each "
                                  "{title: string, bullets: list of strings}."}},
        "required": ["filename"]},
    # Written only inside Jarvis's own artifacts folder, nothing outside it changes,
    # and the person can delete it — see the module docstring.
    risk=Risk.LOW,
    handler=_run,
    wants_context=True,
    tags=frozenset({"core"}),
    summarize=lambda args: f'Create "{safe_name(str(args.get("filename") or ""))}"?',
    timeout_s=60.0,
)
