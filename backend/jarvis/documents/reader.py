"""Reading .docx and .xlsx back into plain text.

Two jobs, and the second is the important one: it lets a generated document be
VERIFIED by being re-opened with an independently written reader. "The file
exists and is 4KB" is not evidence that anything can open it.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


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


def read_document(path: Path | str) -> str:
    """Plain text from whichever of the two this is. Raises for anything else,
    rather than returning an empty string that reads like an empty document."""
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        return read_docx(path)
    if suffix == ".xlsx":
        return "\n".join("\t".join(row) for row in read_xlsx(path))
    raise ValueError(f"I can't read a {suffix or 'file with no extension'} document.")
