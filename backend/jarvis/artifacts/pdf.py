"""A real, text PDF with no dependency — the same spirit as `office.py`.

Deliberately small: headings (`#`, `##`, `###`), paragraphs, bullets (`-`/`*`)
and fenced code blocks, wrapped to the page and broken across as many A4 pages
as the text needs. It uses the fourteen fonts every PDF reader already has, so
nothing is embedded; those fonts only carry the Windows-1252 character set, so a
character outside it (most non-Latin scripts, emoji) is shown as "?" and the
count of such characters is returned, so the caller can SAY so rather than hand
over a PDF that quietly lost text. For anything richer, a .docx is the better
format, and the tool's description says so.

Verified the same way as the Office writers: re-opened by an independently
written reader (`documents/reader.py::read_pdf`), which checks the cross-reference
table points at the objects it claims to and reads the text back out.
"""

from __future__ import annotations

from pathlib import Path

PAGE_W, PAGE_H = 595.28, 841.89  # A4, in points
MARGIN = 56.0
BODY_SIZE = 11.0
CODE_SIZE = 9.5
HEADINGS = {"# ": 20.0, "## ": 16.0, "### ": 13.0}

#: Helvetica advance widths (thousandths of an em) for the characters that differ
#: most from the average; everything else uses the class default below. Wrapping
#: slightly early is harmless; running off the page is not, so estimates round up.
_NARROW = {c: 278 for c in " .,:;!|'ijlI[]()/\\-ft"} | {"r": 333, "\"": 355}
_WIDE = {"m": 833, "w": 722, "M": 833, "W": 944, "@": 1015, "%": 889}


def _width(text: str, size: float, bold: bool = False, mono: bool = False) -> float:
    if mono:
        return len(text) * 600 * size / 1000
    total = 0
    for ch in text:
        if ch in _WIDE:
            total += _WIDE[ch]
        elif ch in _NARROW:
            total += _NARROW[ch]
        elif ch.isupper():
            total += 667
        else:
            total += 556
    return total * (1.08 if bold else 1.03) * size / 1000


def _encode(text: str) -> tuple[bytes, int]:
    """Windows-1252 bytes, escaped for a PDF string; plus how many were lost."""
    out = bytearray()
    lost = 0
    for ch in text:
        try:
            byte = ch.encode("cp1252")
        except UnicodeEncodeError:
            byte, lost = b"?", lost + 1
        if byte in (b"\\", b"(", b")"):
            out += b"\\" + byte
        else:
            out += byte
    return bytes(out), lost


def _wrap(text: str, size: float, width: float, bold: bool, mono: bool) -> list[str]:
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if _width(candidate, size, bold, mono) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        # A single word wider than the line is broken by characters.
        while _width(word, size, bold, mono) > width and len(word) > 1:
            cut = len(word)
            while cut > 1 and _width(word[:cut], size, bold, mono) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    lines.append(current)
    return lines


def _layout(text: str) -> list[list[tuple[str, float, str, float, float]]]:
    """Pages of (font, size, text, x, y) lines, top to bottom."""
    width = PAGE_W - 2 * MARGIN
    pages: list[list[tuple[str, float, str, float, float]]] = [[]]
    y = PAGE_H - MARGIN

    def emit(font: str, size: float, line: str, indent: float = 0.0, gap: float = 1.35) -> None:
        nonlocal y
        step = size * gap
        if y - step < MARGIN:
            pages.append([])
            y = PAGE_H - MARGIN
        y -= step
        pages[-1].append((font, size, line, MARGIN + indent, y))

    in_code = False
    for raw in (text or "").replace("\r\n", "\n").replace("\t", "    ").split("\n"):
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            for piece in _wrap(raw, CODE_SIZE, width - 12, False, True):
                emit("F3", CODE_SIZE, piece, indent=12, gap=1.3)
            continue
        line = raw.rstrip()
        if not line.strip():
            y -= BODY_SIZE * 0.6
            continue
        heading = next((size for mark, size in HEADINGS.items() if line.startswith(mark)), None)
        if heading:
            y -= heading * 0.35
            for piece in _wrap(line.split(" ", 1)[1].strip(), heading, width, True, False):
                emit("F2", heading, piece, gap=1.25)
            y -= heading * 0.2
            continue
        stripped = line.lstrip()
        if stripped[:2] in ("- ", "* "):
            pieces = _wrap(stripped[2:], BODY_SIZE, width - 16, False, False)
            emit("F1", BODY_SIZE, "•", indent=4)
            y += BODY_SIZE * 1.35  # the bullet shares the first line
            for piece in pieces:
                emit("F1", BODY_SIZE, piece, indent=16)
            continue
        for piece in _wrap(line, BODY_SIZE, width, False, False):
            emit("F1", BODY_SIZE, piece)
    return [page for page in pages if page] or [[]]


def write_pdf(path: Path, text: str, title: str | None = None) -> tuple[Path, int]:
    """Write `text` as a PDF. Returns (path, characters that could not be shown)."""
    pages = _layout(text)
    lost = 0
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in once the page tree exists
    tree = add(b"")
    fonts = {
        "F1": add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"),
        "F2": add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"),
        "F3": add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"),
    }
    resources = b"<< /Font << " + b" ".join(
        f"/{name} {num} 0 R".encode() for name, num in fonts.items()) + b" >> >>"
    kids: list[int] = []
    for page in pages:
        ops = []
        for font, size, line, x, y in page:
            encoded, missing = _encode(line)
            lost += missing
            ops.append(b"BT /" + font.encode() + f" {size:g} Tf {x:.2f} {y:.2f} Td (".encode()
                       + encoded + b") Tj ET")
        stream = b"\n".join(ops)
        content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
                      + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                        f"/Contents {content} 0 R /Resources ".encode() + resources + b" >>"))
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    objects[tree - 1] = (f"<< /Type /Pages /Count {len(kids)} /Kids ["
                         + " ".join(f"{k} 0 R" for k in kids) + "] >>").encode()
    info = add(b"<< /Title (" + _encode(title or Path(path).stem)[0] + b") /Producer (Jarvis) >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path, lost
