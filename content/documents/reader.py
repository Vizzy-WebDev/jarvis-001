"""Reading .docx, .xlsx and .pptx back into plain text.

Two jobs, and the second is the important one: it lets a generated document be
VERIFIED by being re-opened with an independently written reader. "The file
exists and is 4KB" is not evidence that anything can open it.

`read_pptx()` is more than "does a slide's XML parse" on purpose: a real, earlier
build's own pptx writer disclosed a verification gap right here — checking a
slide's own text without ever confirming the slide MASTER it depends on actually
resolves to a real layout and theme. This reader walks that whole chain
(`presentation.xml` -> its declared master and slides -> the master's own
layout/theme) and raises the moment any hop points at something missing, rather
than reporting success on a package a real slide master/layout mismatch would
still make PowerPoint report as needing repair.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
#: The namespace an `r:id` ATTRIBUTE lives in (a `<p:sldMasterId r:id="...">`,
#: a `<p:sldId r:id="...">`) — NOT a `.rels` file's own root element namespace.
#: See `documents/office.py`'s own `R` constant for the mixup this corrects.
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def read_docx(path: Path | str) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    paragraphs = []
    for paragraph in root.iter(f"{W}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{W}t"))
        paragraphs.append(text)
    return "\n".join(paragraphs).strip()


def read_xlsx(path: Path | str) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("xl/worksheets/sheet1.xml")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in item.iter(f"{S}t"))
                      for item in shared_root.iter(f"{S}si")]

    root = ElementTree.fromstring(xml)
    rows: list[list[str]] = []
    for row in root.iter(f"{S}row"):
        values: list[str] = []
        for cell in row.iter(f"{S}c"):
            kind = cell.get("t")
            if kind == "inlineStr":
                values.append("".join(t.text or "" for t in cell.iter(f"{S}t")))
            elif kind == "s":
                index = cell.findtext(f"{S}v")
                values.append(shared[int(index)] if index is not None and
                              index.isdigit() and int(index) < len(shared) else "")
            else:
                values.append(cell.findtext(f"{S}v") or "")
        rows.append(values)
    return rows


def _rels_entries(archive: zipfile.ZipFile, rels_path: str) -> list[dict[str, str]]:
    if rels_path not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read(rels_path))
    return [{"id": rel.get("Id") or "", "type": rel.get("Type") or "",
             "target": rel.get("Target") or ""} for rel in root]


def _rel_target_by_id(entries: list[dict[str, str]], rid: str | None) -> str:
    for entry in entries:
        if entry["id"] == rid:
            return entry["target"]
    raise KeyError(f"no relationship with id {rid!r}")


def _rel_target_by_type_suffix(entries: list[dict[str, str]], suffix: str) -> str:
    for entry in entries:
        if entry["type"].endswith(f"/{suffix}"):
            return entry["target"]
    raise KeyError(f"no relationship of type {suffix!r}")


def _resolve(base_dir: str, target: str) -> str:
    """A relationship `Target` is relative to the part's OWN directory, not the
    archive root — `../slideLayouts/x.xml` from `ppt/slideMasters/_rels/`
    resolves against `ppt/slideMasters`, not `ppt`."""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def read_pptx(path: Path | str) -> list[str]:
    """One plain-text string per slide, in the deck's own declared order — see
    this file's own header for why the chain walk matters as much as the text.
    Raises (`KeyError`/`ValueError`/`ElementTree.ParseError`) the moment any
    hop of presentation -> master -> layout/theme, or presentation -> slide,
    fails to resolve to a real, parseable part.
    """
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())

        presentation = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
        master_rid = next((m.get(f"{R}id") for m in presentation.iter(f"{P}sldMasterId")), None)
        slide_rids = [s.get(f"{R}id") for s in presentation.iter(f"{P}sldId")]
        if not master_rid or not slide_rids:
            raise ValueError("the presentation declares no master, or no slides")

        pres_rels = _rels_entries(archive, "ppt/_rels/presentation.xml.rels")
        master_path = _resolve("ppt", _rel_target_by_id(pres_rels, master_rid))
        slide_paths = [_resolve("ppt", _rel_target_by_id(pres_rels, rid)) for rid in slide_rids]
        for part in (master_path, *slide_paths):
            if part not in names:
                raise KeyError(f"presentation.xml points at a missing part: {part}")

        # The chain a real, disclosed gap in an earlier build never actually
        # checked: the master must resolve to a REAL layout and REAL theme,
        # both present in the archive and at least well-formed XML.
        master_dir = posixpath.dirname(master_path)
        master_rels_path = posixpath.join(
            master_dir, "_rels", posixpath.basename(master_path) + ".rels")
        master_rels = _rels_entries(archive, master_rels_path)
        layout_path = _resolve(master_dir, _rel_target_by_type_suffix(master_rels, "slideLayout"))
        theme_path = _resolve(master_dir, _rel_target_by_type_suffix(master_rels, "theme"))
        for part in (layout_path, theme_path):
            if part not in names:
                raise KeyError(f"the slide master points at a missing part: {part}")
            ElementTree.fromstring(archive.read(part))  # must at least parse

        return [
            "\n".join(t.text.strip() for t in
                      ElementTree.fromstring(archive.read(part)).iter(f"{A}t")
                      if (t.text or "").strip())
            for part in slide_paths
        ]


_PDF_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_PDF_STREAM = re.compile(rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream", re.S)
_PDF_TEXT = re.compile(rb"\(((?:\\.|[^\\)])*)\)\s*Tj")


def _pdf_string(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i:i + 1] == b"\\" and i + 1 < len(raw):
            nxt = raw[i + 1:i + 2]
            out += {b"n": b"\n", b"r": b"\r", b"t": b"\t"}.get(nxt, nxt)
            i += 2
        else:
            out += raw[i:i + 1]
            i += 1
    return out.decode("cp1252", errors="replace")


def read_pdf(path: Path | str) -> list[str]:
    """The text of each page's content, in order. Raises when the file is not a
    PDF a reader could open: no header, no end marker, a cross-reference table
    whose offsets do not land on the objects it names, or no pages at all.

    Lenient on purpose about what it cannot follow: a PDF with compressed
    cross-reference streams (made by other software) is structurally checked
    as far as this reader goes and not rejected for using a newer feature.
    """
    import zlib

    data = Path(path).read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError("it does not start like a PDF")
    if b"%%EOF" not in data[-1024:]:
        raise ValueError("it has no end-of-file marker")
    start = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data)
    if not start:
        raise ValueError("it has no cross-reference pointer")
    xref_at = int(start.group(1))
    if data[xref_at:xref_at + 4] == b"xref":
        header = re.match(rb"xref\s+(\d+)\s+(\d+)\s+", data[xref_at:])
        if not header:
            raise ValueError("its cross-reference table is malformed")
        first, count = int(header.group(1)), int(header.group(2))
        table = data[xref_at + header.end():]
        for n in range(count):
            entry = table[n * 20:(n + 1) * 20]
            if len(entry) < 18:
                raise ValueError("its cross-reference table is cut short")
            if entry[17:18] != b"n":
                continue
            offset = int(entry[:10])
            found = _PDF_OBJ.match(data, offset)
            if not found or int(found.group(1)) != first + n:
                raise ValueError(f"object {first + n} is not where the file says it is")
    elif not _PDF_OBJ.match(data, xref_at):
        raise ValueError("its cross-reference pointer leads nowhere")
    if not re.search(rb"/Type\s*/Page\b(?!s)", data):
        raise ValueError("it has no pages")

    texts: list[str] = []
    for dictionary, body in _PDF_STREAM.findall(data):
        if b"/FlateDecode" in dictionary:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                continue
        found = [_pdf_string(t) for t in _PDF_TEXT.findall(body)]
        if found:
            texts.append("\n".join(found))
    return texts


def read_document(path: Path | str) -> str:
    """Plain text from whichever of these this is. Raises for anything
    else, rather than returning an empty string that reads like an empty
    document."""
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        return read_docx(path)
    if suffix == ".xlsx":
        return "\n".join("\t".join(row) for row in read_xlsx(path))
    if suffix == ".pptx":
        return "\n\n".join(read_pptx(path))
    if suffix == ".pdf":
        return "\n\n".join(read_pdf(path))
    raise ValueError(f"I can't read a {suffix or 'file with no extension'} document.")
