"""Answering a question against a WHOLE workbook, not the part that fit.

A small spreadsheet is read straight into the message as a table — simpler, and
it works on every model, so it stays the default. This is what a big workbook's
truncation note points at: rather than pasting more rows (which is a context
window problem that more rows do not fix), it looks at the sheet's SHAPE, writes
a short script, and runs it against the real, full file in the sandbox — the way
an analyst would, instead of guessing from a sample.

**Deliberately not confirm-gated.** Re-confirming every follow-up question about
one spreadsheet would make it unusable, and the script is generated from the
user's own question against a file they attached moments ago, with no access to
anything else. Returning the script alongside the answer is the visibility that
replaces the prompt: they can see exactly how the number was reached.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from ..capabilities import CapabilitySpec, Risk

SAMPLE_ROWS = 5
SANDBOX_TIMEOUT_S = 20.0

_FENCE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.S | re.I)


def _extract_code(text: str) -> str:
    fenced = _FENCE.search(text or "")
    return (fenced.group(1) if fenced else (text or "")).strip()


def _to_csv(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue()


def _write_script(*, sheet: str, header: str, sample: list[str], row_count: int,
                  question: str) -> Any:
    from ..ai import ask_model

    prompt = (
        f'A CSV file named "data.csv" sits in the current folder — sheet "{sheet}" from a '
        f"spreadsheet, {row_count} data rows not counting the header. It is NOT pasted below "
        f"in full, only a sample, because the real file is too big for this prompt: your "
        f"script must read the actual file, not this sample.\n\n"
        f"Header: {header}\nFirst {len(sample)} rows, as a sample of the shape:\n"
        + "\n".join(sample) + "\n\n"
        f"Question to answer: {question}\n\n"
        "Write a complete, self-contained Python 3 script that:\n"
        '- reads "data.csv" using the standard library only (csv, no pandas — it is not '
        "installed)\n"
        "- computes the real answer over the WHOLE file, not the sample\n"
        "- ends with exactly one print() of a short, plain-language final answer (not JSON, "
        "not the raw data)\n"
        "Reply with ONLY the Python code — no explanation, before or after.")
    return ask_model(prompt)


def _run(upload_id: str = "", question: str = "", sheet_name: str = "") -> dict[str, Any]:
    from ..documents import sheet_names, sheet_rows
    from ..sandbox.runner import run_python
    from ..uploads import get_upload

    upload = get_upload(upload_id)
    if upload is None:
        return {"ok": False,
                "error": "I can't find that attachment any more — it may have been cleared. "
                         "Ask them to attach the file again."}
    if not (question or "").strip():
        return {"ok": False, "error": "I didn't catch what they wanted to know about it."}

    try:
        names = sheet_names(upload["path"])
    except Exception as err:  # noqa: BLE001
        return {"ok": False,
                "error": f"That doesn't look like a readable Excel workbook: {err}."}
    if not names:
        return {"ok": False, "error": "That workbook has no readable sheets."}

    sheet = sheet_name if sheet_name in names else names[0]
    try:
        rows = sheet_rows(upload["path"], sheet)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f'Could not read sheet "{sheet}": {err}.'}

    data = _to_csv(rows)
    lines = data.split("\n")
    header = lines[0] if lines else ""
    row_count = max(0, len(rows) - 1)

    written = _write_script(sheet=sheet, header=header,
                            sample=lines[1:1 + SAMPLE_ROWS], row_count=row_count,
                            question=question)
    if not written.ok:
        return {"ok": False, "error": f"I couldn't work out how to analyse that: {written.error}"}

    code = _extract_code(written.text)
    if not code:
        return {"ok": False, "error": "Could not work out how to analyse that file."}

    result = run_python(code, timeout_s=SANDBOX_TIMEOUT_S, keep_files=False,
                        input_files={"data.csv": data})
    if result.ok and result.stdout.strip():
        return {"ok": True, "sheet": sheet, "rowCount": row_count,
                "answer": result.stdout.strip(), "script": code,
                "isolation": result.backend}

    # One retry with the real error fed back — not a loop. Quota is the binding
    # constraint here, so this tries once more and then reports honestly.
    failure = (result.stderr or result.error or "no output")[:2000]
    from ..ai import ask_model

    fixed = ask_model(f"The script you wrote for this task failed:\n\n{code}\n\nError:\n"
                      f"{failure}\n\nFix it and reply with ONLY the corrected Python code.")
    if not fixed.ok:
        return {"ok": False,
                "error": f"The analysis script failed and I couldn't fix it: {failure}"}

    retry_code = _extract_code(fixed.text)
    retry_result = run_python(retry_code, timeout_s=SANDBOX_TIMEOUT_S, keep_files=False,
                              input_files={"data.csv": data})
    if retry_result.ok and retry_result.stdout.strip():
        return {"ok": True, "sheet": sheet, "rowCount": row_count,
                "answer": retry_result.stdout.strip(), "script": retry_code,
                "isolation": retry_result.backend, "retried": True}

    return {"ok": False,
            "error": ("The analysis script failed twice, so I don't have a real answer. "
                      f"The last error was: "
                      f"{(retry_result.stderr or retry_result.error or 'no output')[:400]}")}


SPEC = CapabilitySpec(
    id="builtin.analyze_spreadsheet",
    name="analyze_spreadsheet",
    description=("Answer a question against the FULL contents of an Excel workbook someone "
                 "attached. Use it when a spreadsheet was too large to read in full in the "
                 "conversation — the attachment note says so and gives a reference id — and "
                 "the question needs an exact answer covering every row. Not for a small "
                 "workbook that already read in fully: answer those from what is already "
                 "here."),
    input_schema={"type": "object", "properties": {
        "upload_id": {"type": "string",
                      "description": "The reference id from the attachment note, not a filename."},
        "question": {"type": "string",
                     "description": "Their question about the data, in their own words."},
        "sheet_name": {"type": "string",
                       "description": ("Which sheet, if the workbook has more than one and it "
                                       "matters. Leave out for the first.")}},
        "required": ["upload_id", "question"]},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=120.0,
)
