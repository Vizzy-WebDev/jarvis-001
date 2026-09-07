"""Writing real .docx and .xlsx files, and the ZIP rule that makes them open.

**Every entry name is a hand-built forward-slash string.** Office formats follow
the Open Packaging Conventions, which require forward slashes; an entry name
derived from walking a directory picks up the platform's separator, and on
Windows that produces a file that looks fine, has the right signature, and will
not open in Word at all. Python's zipfile is better behaved than PowerShell here,
but the rule is the same and a test reads the entry names back out of a real
generated file rather than trusting it.

Deliberately minimal documents: a paragraph list and a cell grid. This is not a
document-layout library, and pretending otherwise by accepting styling arguments
that quietly do nothing would be the same dishonesty in a different place.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

CONTENT_TYPES = "[Content_Types].xml"


def _escape(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _write(path: Path, entries: dict[str, str]) -> Path:
    """Write a package. `entries` keys are the exact entry names, forward-slashed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, contents in entries.items():
            assert "\\" not in name, f"entry name must be forward-slashed: {name!r}"
            archive.writestr(name, contents)
    return path


# --- .docx -------------------------------------------------------------------

def write_docx(path: Path, paragraphs: list[str], title: str | None = None) -> Path:
    body = "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{_escape(p)}</w:t></w:r></w:p>"
        for p in ([title] if title else []) + list(paragraphs))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>")
    return _write(path, {
        CONTENT_TYPES: (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>"),
        "word/document.xml": document,
    })


# --- .xlsx -------------------------------------------------------------------

def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def write_xlsx(path: Path, rows: list[list[Any]], sheet_name: str = "Sheet1") -> Path:
    """Inline strings rather than a shared-string table: fewer parts, no index to
    get out of step with the cells that reference it."""
    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            reference = f"{_column_name(c)}{r}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">'
                             f"{_escape(value)}</t></is></c>")
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')

    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    return _write(path, {
        CONTENT_TYPES: (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"),
        "xl/worksheets/sheet1.xml": sheet,
    })


def entry_names(path: Path) -> list[str]:
    """The entry names actually stored — what a test must read to know the
    package is well formed. A file can have the right signature, the right size,
    and entry names no Office application will accept."""
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()
