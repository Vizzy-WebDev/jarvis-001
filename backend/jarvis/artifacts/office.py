"""Writing real .docx, .xlsx and .pptx files, and the ZIP rule that makes them open.

**Every entry name is a hand-built forward-slash string.** Office formats follow
the Open Packaging Conventions, which require forward slashes; an entry name
derived from walking a directory picks up the platform's separator, and on
Windows that produces a file that looks fine, has the right signature, and will
not open in Word at all. Python's zipfile is better behaved than PowerShell here,
but the rule is the same and a test reads the entry names back out of a real
generated file rather than trusting it.

Deliberately minimal documents: a paragraph list, a cell grid, a title-and-bullets
slide deck. This is not a document-layout library, and pretending otherwise by
accepting styling arguments that quietly do nothing would be the same dishonesty in
a different place.
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


# --- .pptx ---------------------------------------------------------------
#
# More parts than .docx/.xlsx ever needed — a real presentation has a
# master/layout/theme chain, not just one content part. `presentation.xml`
# and the per-slide parts vary with content; the master/layout/theme are
# constant boilerplate every time, the same way write_docx's own
# [Content_Types].xml never changes either.

#: Widescreen, in EMU (914400 EMU = 1 inch) — PowerPoint's own current
#: default (13.333in x 7.5in). Not exposed as an option: like write_docx and
#: write_xlsx, this is a minimal writer, not a layout engine.
_SLIDE_CX, _SLIDE_CY = 12192000, 6858000

#: The default "Title and Content" placeholder geometry PowerPoint itself
#: uses on a widescreen slide. Our own slideLayout/slideMaster carry no
#: placeholder geometry of their own (both are otherwise empty — see
#: `_SLIDE_LAYOUT`/`_SLIDE_MASTER` below), so each slide's own shapes need an
#: explicit `<a:xfrm>` or nothing tells a reader where to put them.
_TITLE_XFRM = (838200, 365125, 10515600, 1325563)
_BODY_XFRM = (838200, 1825625, 10515600, 4351338)

_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P_NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')


def _rels_xml(rels: list[tuple[str, str, str]]) -> str:
    """`rels`: `(id, relationship-type-suffix, target)`."""
    body = "".join(
        f'<Relationship Id="{rid}" Type="{_REL_NS}/{kind}" Target="{target}"/>'
        for rid, kind, target in rels)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'relationships">{body}</Relationships>')


def _placeholder_xml(shape_id: int, name: str, ph_type: str, ph_idx: int | None,
                     xfrm: tuple[int, int, int, int], paragraphs: list[str]) -> str:
    idx_attr = f' idx="{ph_idx}"' if ph_idx is not None else ""
    x, y, cx, cy = xfrm
    body = "".join(f"<a:p><a:r><a:t>{_escape(p)}</a:t></a:r></a:p>"
                   for p in paragraphs) or "<a:p/>"
    return (
        "<p:sp>"
        f'<p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        f'<p:nvPr><p:ph type="{ph_type}"{idx_attr}/></p:nvPr></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm></p:spPr>'
        f"<p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody>"
        "</p:sp>"
    )


def _slide_xml(title: str | None, bullets: list[str]) -> str:
    title_shape = _placeholder_xml(2, "Title", "title", None, _TITLE_XFRM,
                                   [title] if title else [])
    body_shape = _placeholder_xml(3, "Content", "body", 1, _BODY_XFRM, list(bullets))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<p:sld {_P_NS}><p:cSld><p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        f"<p:grpSpPr/>{title_shape}{body_shape}"
        "</p:spTree></p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>"
        "</p:sld>"
    )


_SP_TREE_EMPTY = ('<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
                  '<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree>')

#: Both otherwise-empty on purpose: no placeholder geometry of their own to
#: inherit (every slide carries its own explicit `<a:xfrm>` instead — see
#: `_TITLE_XFRM`/`_BODY_XFRM` above), and no visual styling this writer claims
#: to offer. Constant, the same every deck, like `write_docx`'s own
#: `[Content_Types].xml`.
_SLIDE_MASTER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<p:sldMaster {_P_NS}><p:cSld>{_SP_TREE_EMPTY}</p:cSld>'
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
    'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
    'accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rIdLayout"/></p:sldLayoutIdLst>'
    "</p:sldMaster>"
)

_SLIDE_LAYOUT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<p:sldLayout {_P_NS} type="blank" preserve="1">'
    f'<p:cSld name="Blank">{_SP_TREE_EMPTY}</p:cSld>'
    "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>"
    "</p:sldLayout>"
)

#: A real, minimal theme — PowerPoint's own strict schema requires a
#: `<a:fmtScheme>` with fill/line/effect/bgFill style lists present (three
#: entries each), not an empty placeholder. Fixed, one theme for every deck
#: this writer produces.
_THEME = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Jarvis">'
    '<a:themeElements>'
    '<a:clrScheme name="Jarvis">'
    '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="44546A"/></a:dk2>'
    '<a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
    '<a:accent1><a:srgbClr val="4472C4"/></a:accent1>'
    '<a:accent2><a:srgbClr val="ED7D31"/></a:accent2>'
    '<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>'
    '<a:accent4><a:srgbClr val="FFC000"/></a:accent4>'
    '<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5>'
    '<a:accent6><a:srgbClr val="70AD47"/></a:accent6>'
    '<a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
    '<a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
    "</a:clrScheme>"
    '<a:fontScheme name="Jarvis">'
    '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/>'
    "</a:majorFont>"
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/>'
    "</a:minorFont>"
    "</a:fontScheme>"
    '<a:fmtScheme name="Jarvis">'
    "<a:fillStyleLst>" + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3 +
    "</a:fillStyleLst>"
    "<a:lnStyleLst>" +
    '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>' * 3 +
    "</a:lnStyleLst>"
    "<a:effectStyleLst>" + "<a:effectStyle><a:effectLst/></a:effectStyle>" * 3 +
    "</a:effectStyleLst>"
    "<a:bgFillStyleLst>" + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3 +
    "</a:bgFillStyleLst>"
    "</a:fmtScheme>"
    "</a:themeElements>"
    "</a:theme>"
)


def _pptx_content_types(slide_count: int) -> str:
    overrides = (
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.theme+xml"/>'
    )
    slides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, slide_count + 1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        f"{overrides}{slides}</Types>"
    )


def _presentation_xml(slide_count: int) -> str:
    # IDs below 256/2147483648 are reserved by the spec — every real deck's
    # own sldId/sldMasterId/sldLayoutId starts exactly here.
    slide_ids = "".join(
        f'<p:sldId id="{255 + i}" r:id="rId{i}"/>' for i in range(1, slide_count + 1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<p:presentation {_P_NS}>"
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rIdMaster"/>'
        "</p:sldMasterIdLst>"
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        f'<p:sldSz cx="{_SLIDE_CX}" cy="{_SLIDE_CY}"/>'
        '<p:notesSz cx="6858000" cy="9144000"/>'
        "</p:presentation>"
    )


def write_pptx(path: Path, slides: list[dict[str, Any]]) -> Path:
    """`slides`: `[{"title": str | None, "bullets": list[str]}, ...]`.

    One fixed slide layout ("title + body text"), one fixed theme, widescreen.
    Deliberately NOT attempting real bulleted-list glyphs (plain paragraphs,
    same as `write_docx` never emitting `<w:numPr>` list numbering either),
    speaker notes, or images — a minimal writer, not a layout engine, the same
    honesty `write_docx`/`write_xlsx` already practice.
    """
    n = len(slides)
    entries = {
        CONTENT_TYPES: _pptx_content_types(n),
        "_rels/.rels": _rels_xml([("rId1", "officeDocument", "ppt/presentation.xml")]),
        "ppt/presentation.xml": _presentation_xml(n),
        "ppt/_rels/presentation.xml.rels": _rels_xml([
            ("rIdMaster", "slideMaster", "slideMasters/slideMaster1.xml"),
            *[(f"rId{i}", "slide", f"slides/slide{i}.xml") for i in range(1, n + 1)],
        ]),
        "ppt/slideMasters/slideMaster1.xml": _SLIDE_MASTER,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": _rels_xml([
            ("rIdLayout", "slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rIdTheme", "theme", "../theme/theme1.xml"),
        ]),
        "ppt/slideLayouts/slideLayout1.xml": _SLIDE_LAYOUT,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _rels_xml([
            ("rId1", "slideMaster", "../slideMasters/slideMaster1.xml"),
        ]),
        "ppt/theme/theme1.xml": _THEME,
    }
    for i, slide in enumerate(slides, start=1):
        entries[f"ppt/slides/slide{i}.xml"] = _slide_xml(
            slide.get("title"), list(slide.get("bullets") or []))
        entries[f"ppt/slides/_rels/slide{i}.xml.rels"] = _rels_xml([
            ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
        ])
    return _write(path, entries)


def entry_names(path: Path) -> list[str]:
    """The entry names actually stored — what a test must read to know the
    package is well formed. A file can have the right signature, the right size,
    and entry names no Office application will accept."""
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()
