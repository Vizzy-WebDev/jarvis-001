"""Producing a real file the user can open.

Deliberately NOT marked meta: in the original this tool was, and a background job
strips meta tools before anything else applies — so a job asked to produce a
report structurally could not produce one. That is the kind of gap nobody finds
by reading the code.

One capability ceiling, stated in the description because a model cannot infer
it: there is no image generation here. No adapter does it and no image library
exists in this project's dependencies, so asking for a photograph produces
nothing, and saying so up front is better than producing an empty file.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ..artifacts import keep, safe_name, write_docx, write_xlsx
from ..capabilities import CapabilitySpec, Risk

TEXT_FORMATS = {".txt", ".md", ".csv", ".json", ".html", ".svg"}


def _run(filename: str = "", content: str = "", rows: list[Any] | None = None,
         paragraphs: list[str] | None = None) -> dict[str, Any]:
    name = safe_name(filename)
    if not name or "." not in name:
        return {"ok": False, "error": "I need a filename with an extension."}
    suffix = Path(name).suffix.lower()
    staging = Path(tempfile.mkdtemp(prefix="jarvis-artifact-")) / name

    try:
        if suffix == ".docx":
            body = paragraphs or [line for line in (content or "").split("\n\n") if line.strip()]
            if not body:
                return {"ok": False, "error": "There's nothing to put in the document."}
            write_docx(staging, body)
        elif suffix == ".xlsx":
            grid = rows or _rows_from_csv(content)
            if not grid:
                return {"ok": False, "error": "There's nothing to put in the spreadsheet."}
            write_xlsx(staging, grid)
        elif suffix in TEXT_FORMATS:
            # Written exactly as given: for these, the content IS the file, and
            # reformatting it would be changing what was asked for.
            staging.parent.mkdir(parents=True, exist_ok=True)
            staging.write_text(content or "", encoding="utf-8")
        else:
            return {"ok": False,
                    "error": f"I can't make a {suffix} file. I can do documents (.docx), "
                             "spreadsheets (.xlsx), and plain text formats."}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f"I couldn't build that file: {err}"}

    try:
        artifact = keep(staging, name=name)
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    return {"ok": True, **artifact.as_result(),
            "speak": f"{artifact.name} is ready — it's here in the conversation."}


def _rows_from_csv(content: str) -> list[list[str]]:
    import csv
    import io

    if not (content or "").strip():
        return []
    return [row for row in csv.reader(io.StringIO(content))]


SPEC = CapabilitySpec(
    id="builtin.create_artifact", name="create_artifact",
    description=("Produce a real file the user can open — a document (.docx), a spreadsheet "
                 "(.xlsx), or plain text (.txt, .md, .csv, .json, .html, .svg). Cannot make "
                 "images or photographs: there is no image generation here at all."),
    input_schema={"type": "object", "properties": {
        "filename": {"type": "string", "description": "Including the extension."},
        "content": {"type": "string",
                    "description": "The text. For .xlsx, CSV rows if `rows` is not given."},
        "rows": {"type": "array", "description": "For .xlsx: a list of rows, each a list."},
        "paragraphs": {"type": "array", "description": "For .docx: one string per paragraph."}},
        "required": ["filename"]},
    # It writes a file into the user's own space, and a wrong one is clutter
    # rather than damage — but it is still a write, so it is not LOW.
    risk=Risk.MEDIUM,
    handler=_run,
    summarize=lambda args: f'Create "{safe_name(str(args.get("filename") or ""))}"?',
    timeout_s=60.0,
)
