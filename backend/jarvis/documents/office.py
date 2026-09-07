"""Word, Excel and PowerPoint, read into Markdown.

Separate from `reader.py` on purpose. That file is the deliberately minimal,
independently-written reader the artifact writers are VERIFIED against — a round
trip through the same code proves nothing, so it stays small and stays honest.
This one is richer: multiple sheets, tables, slide order, embedded images, and a
row budget, because an attachment has to fit inside a turn's context.

No new dependency. These formats are zip archives of XML, and the standard
library reads both.
"""

from __future__ import annotations

import base64
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/package/2006/relationships}"

OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})

#: Enough of a workbook to answer most questions, far short of eating a turn.
#: The caller passes its own number: an attachment and a full analysis have very
#: different amounts of room.
DEFAULT_MAX_ROWS = 300

#: An embedded picture only rides along if it is small enough to inline; past
#: this it is described rather than sent, which beats failing the whole read.
IMAGE_MAX_BYTES = 2 * 1024 * 1024
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp"}


def is_office_document(path: Path | str) -> bool:
    return Path(path).suffix.lower() in OFFICE_SUFFIXES


def _images(archive: zipfile.ZipFile, prefix: str) -> list[dict[str, Any]]:
    found = []
    for name in archive.namelist():
        if not name.startswith(prefix):
            continue
        mime = IMAGE_MIME.get(Path(name).suffix.lower())
        if not mime:
            continue
        data = archive.read(name)
        if len(data) > IMAGE_MAX_BYTES:
            continue
        found.append({"kind": "image", "mimeType": mime,
                      "dataBase64": base64.b64encode(data).decode("ascii")})
    return found


# --- Word ---------------------------------------------------------------------

def _paragraph_text(node: Any) -> str:
    return "".join(t.text or "" for t in node.iter(f"{W}t"))


def _heading_level(node: Any) -> int:
    style = node.find(f"{W}pPr/{W}pStyle")
    name = (style.get(f"{W}val") if style is not None else "") or ""
    if name.lower().startswith("heading"):
        digits = "".join(c for c in name if c.isdigit())
        return min(int(digits), 6) if digits else 1
    return 0


def _is_list_item(node: Any) -> bool:
    return node.find(f"{W}pPr/{W}numPr") is not None


def docx_to_markdown(path: Path | str) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
        images = _images(archive, "word/media/")

    lines: list[str] = []
    body = root.find(f"{W}body") or root
    for node in body:
        tag = node.tag
        if tag == f"{W}p":
            text = _paragraph_text(node).strip()
            if not text:
                continue
            level = _heading_level(node)
            if level:
                lines.append(f"{'#' * level} {text}")
            elif _is_list_item(node):
                lines.append(f"- {text}")
            else:
                lines.append(text)
        elif tag == f"{W}tbl":
            rows = [[" ".join(_paragraph_text(p).strip() for p in cell.iter(f"{W}p")).strip()
                     for cell in row.iter(f"{W}tc")]
                    for row in node.iter(f"{W}tr")]
            lines.extend(_markdown_table(rows))
    return {"markdown": "\n\n".join(lines).strip(), "images": images}


def _markdown_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    head = "| " + " | ".join(padded[0]) + " |"
    rule = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(r) + " |" for r in padded[1:]]
    return [head, rule, *body] if body else [head, rule]


# --- Excel --------------------------------------------------------------------

def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in item.iter(f"{S}t")) for item in root.iter(f"{S}si")]


def _sheet_parts(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(name, archive path) per sheet, in the workbook's own order.

    Resolved through the workbook relationships rather than by guessing
    `sheet1.xml`, `sheet2.xml`: a workbook edited over time does not keep those
    in step with the tab order, and reading by guess silently returns the wrong
    sheet under the right name.
    """
    names = [(s.get("name") or "Sheet", s.get(f"{R}id"))
             for s in ElementTree.fromstring(
                 archive.read("xl/workbook.xml")).iter(f"{S}sheet")]
    targets: dict[str, str] = {}
    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path in archive.namelist():
        for rel in ElementTree.fromstring(archive.read(rels_path)):
            targets[rel.get("Id")] = rel.get("Target") or ""

    parts = []
    for index, (name, rel_id) in enumerate(names, start=1):
        target = targets.get(rel_id or "", f"worksheets/sheet{index}.xml").lstrip("/")
        path = target if target.startswith("xl/") else f"xl/{target}"
        if path in archive.namelist():
            parts.append((name, path))
    return parts


def _rows_of(archive: zipfile.ZipFile, part: str, shared: list[str]) -> list[list[str]]:
    root = ElementTree.fromstring(archive.read(part))
    rows: list[list[str]] = []
    for row in root.iter(f"{S}row"):
        values: list[str] = []
        for cell in row.iter(f"{S}c"):
            kind = cell.get("t")
            if kind == "inlineStr":
                values.append("".join(t.text or "" for t in cell.iter(f"{S}t")))
            elif kind == "s":
                index = cell.findtext(f"{S}v")
                values.append(shared[int(index)]
                              if index is not None and index.isdigit() and int(index) < len(shared)
                              else "")
            else:
                values.append(cell.findtext(f"{S}v") or "")
        rows.append(values)
    return rows


def sheet_names(path: Path | str) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return [name for name, _ in _sheet_parts(archive)]


def sheet_rows(path: Path | str, sheet: str | None = None) -> list[list[str]]:
    """Every row of one sheet, with no limit — the analysis path, where the
    point is to work over the whole thing rather than a readable excerpt."""
    with zipfile.ZipFile(path) as archive:
        parts = _sheet_parts(archive)
        if not parts:
            return []
        chosen = next((p for name, p in parts if name == sheet), None) or parts[0][1]
        return _rows_of(archive, chosen, _shared_strings(archive))


def xlsx_to_markdown(path: Path | str, *,
                     max_rows_per_sheet: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        parts = _sheet_parts(archive)
        blocks: list[str] = []
        truncated = False
        for name, part in parts:
            rows = _rows_of(archive, part, shared)
            kept = rows[:max_rows_per_sheet]
            truncated = truncated or len(rows) > len(kept)
            blocks.append(f"## {name}")
            if not kept:
                blocks.append("*(empty)*")
                continue
            blocks.extend(_markdown_table(kept))
            if len(rows) > len(kept):
                blocks.append(f"*({len(rows) - len(kept)} more rows not shown)*")
    return {"markdown": "\n\n".join(blocks).strip(), "truncated": truncated,
            "sheetCount": len(parts)}


# --- PowerPoint ---------------------------------------------------------------

def _slide_order(archive: zipfile.ZipFile) -> list[str]:
    """Slides in the deck's real order, from the presentation relationships —
    `slide10.xml` sorts before `slide2.xml` by name, which is exactly the kind
    of wrong-but-plausible ordering nobody notices in a summary."""
    names = [s.get(f"{R}id") for s in ElementTree.fromstring(
        archive.read("ppt/presentation.xml")).iter(f"{P}sldId")]
    targets: dict[str, str] = {}
    rels = "ppt/_rels/presentation.xml.rels"
    if rels in archive.namelist():
        for rel in ElementTree.fromstring(archive.read(rels)):
            targets[rel.get("Id")] = rel.get("Target") or ""

    ordered = []
    for rel_id in names:
        target = targets.get(rel_id or "", "").lstrip("/")
        if not target:
            continue
        path = target if target.startswith("ppt/") else f"ppt/{target}"
        if path in archive.namelist():
            ordered.append(path)
    if ordered:
        return ordered
    return sorted(n for n in archive.namelist()
                  if n.startswith("ppt/slides/slide") and n.endswith(".xml"))


def pptx_to_markdown(path: Path | str) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        slides = _slide_order(archive)
        images = _images(archive, "ppt/media/")
        blocks = []
        for number, part in enumerate(slides, start=1):
            root = ElementTree.fromstring(archive.read(part))
            lines = [t.text.strip() for t in root.iter(f"{A}t") if (t.text or "").strip()]
            blocks.append(f"## Slide {number}")
            blocks.extend(lines or ["*(no text on this slide)*"])
    return {"markdown": "\n\n".join(blocks).strip(), "images": images,
            "slideCount": len(slides)}


# --- the one seam callers use -------------------------------------------------

def extract_document(path: Path | str, *,
                     max_rows_per_sheet: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """`{ok, markdown, images, note}` for a Word, Excel or PowerPoint file.

    `note` carries a plain-language truncation message when there is one, so the
    caller can say "there is more than this" rather than presenting an excerpt
    as the whole document.
    """
    target = Path(path)
    suffix = target.suffix.lower()
    if not target.exists():
        return {"ok": False, "error": "I couldn't find that file — it may have been cleared."}

    try:
        if suffix == ".docx":
            return {"ok": True, **docx_to_markdown(target), "note": None}
        if suffix == ".pptx":
            return {"ok": True, **pptx_to_markdown(target), "note": None}
        if suffix == ".xlsx":
            read = xlsx_to_markdown(target, max_rows_per_sheet=max_rows_per_sheet)
            note = ("One or more sheets were too big to include in full here — ask me to "
                    "analyse the whole file for an answer that covers every row."
                    if read["truncated"] else None)
            return {"ok": True, "markdown": read["markdown"], "images": [], "note": note,
                    "truncated": read["truncated"], "sheetCount": read["sheetCount"]}
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as err:
        return {"ok": False, "error": f"That file could not be read: {err}"}

    return {"ok": False,
            "error": "That is not a Word, Excel or PowerPoint file this reader recognises."}
