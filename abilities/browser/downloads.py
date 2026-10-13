"""PDFs the browser downloads: caught, checked, and kept until they are imported.

Ported from OpenMuse's `apps/worker/src/downloads.ts` (MIT licence, CopilotKit), with
the same rules and the same shape:

* Only a PDF is kept - the first five bytes must be `%PDF-`, whatever the site called
  the file. Anything else is reported, never silently dropped: a failure record says
  what was refused and why, so "download the spreadsheet" does not end in a shrug.
* At most 10 MiB each and 20 per session. A transfer that grows past the cap is
  cancelled while it is still arriving, not after it has filled the disk.
* The outcome is journalled BEFORE the transfer starts (`pending`), so a restart that
  interrupts one reports it as failed instead of losing track of it.
* The latest 100 failures are kept; older ones are forgotten.

Files live in the session's own folder, beside its profile, under the data directory.
Importing one into Jarvis's artifacts is a separate step (`imports.py`): capture is
the browser's job, keeping a file is Jarvis's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_PER_SESSION = 20
MAX_FAILURES = 100
_PDF_MAGIC = b"%PDF-"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value), "utf-8")
    os.replace(temp, path)


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._ -]", "_", name or "")[:180] or "download"


def list_downloads(directory: Path) -> list[dict[str, Any]]:
    """The PDFs this session holds, oldest first."""
    folder = directory / "downloads"
    found: list[dict[str, Any]] = []
    if folder.is_dir():
        for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                item = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(item, dict) and (folder / f"{item.get('id')}.pdf").is_file():
                found.append(item)
    return found


def download_path(directory: Path, download_id: str) -> Path | None:
    """Where one downloaded PDF is. Only an id this module made is accepted: the name
    of a file on disk is never built from anything else."""
    if not re.fullmatch(r"[0-9a-f-]{36}", str(download_id)):
        return None
    path = directory / "downloads" / f"{download_id}.pdf"
    return path if path.is_file() else None


def read_failures(directory: Path, recover_interrupted: bool = False) -> list[dict[str, Any]]:
    """Refused or interrupted downloads, newest first. With `recover_interrupted`, a
    transfer still marked pending (the process ended mid-way) becomes a failure, and one
    whose file was in fact published is cleared."""
    folder = directory / "download-outcomes"
    folder.mkdir(parents=True, exist_ok=True)
    failures: list[dict[str, Any]] = []
    for path in folder.glob("*.json"):
        try:
            outcome = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if outcome.get("status") == "pending" and recover_interrupted:
            if (directory / "downloads" / f"{outcome.get('id')}.json").exists():
                path.unlink(missing_ok=True)
                continue
            for leftover in (f"{outcome.get('id')}.pdf", f"{outcome.get('id')}.json.tmp"):
                (directory / "downloads" / leftover).unlink(missing_ok=True)
            outcome["status"] = "failed"
            _write_json(path, outcome)
        if outcome.get("status") == "failed":
            failures.append({k: v for k, v in outcome.items() if k != "status"})
    return sorted(failures, key=lambda f: f.get("createdAt", ""), reverse=True)


async def capture_pdf_download(*, directory: Path, temp_dir: Path, download: Any,
                               limit_reached: bool) -> None:
    """Take one download Chromium has started and keep it if it is a PDF within limits."""
    folder = directory / "downloads"
    outcomes = directory / "download-outcomes"
    folder.mkdir(parents=True, exist_ok=True)
    outcomes.mkdir(parents=True, exist_ok=True)
    download_id = str(uuid.uuid4())
    name = _safe_name(download.suggested_filename)
    outcome: dict[str, Any] = {
        "id": download_id, "name": name, "status": "pending", "code": "DOWNLOAD_INTERRUPTED",
        "message": "The download was interrupted or could not be retrieved. Try downloading "
                   "it again.", "createdAt": _now()}
    outcome_path = outcomes / f"{download_id}.json"
    destination = folder / f"{download_id}.pdf"
    _write_json(outcome_path, outcome)           # journalled first: a restart can report it

    oversized = False
    published = False

    async def watch() -> None:
        nonlocal oversized
        while True:
            await asyncio.sleep(0.1)
            try:
                for item in temp_dir.iterdir():
                    if item.is_file() and item.stat().st_size > MAX_DOWNLOAD_BYTES:
                        oversized = True
                        await download.cancel()
                        return
            except OSError:
                pass

    monitor = asyncio.ensure_future(watch())
    try:
        if limit_reached:
            outcome.update(code="DOWNLOAD_LIMIT",
                           message=f"This window has reached its {MAX_PER_SESSION} PDF "
                                   "download limit.")
            await download.cancel()
            raise RuntimeError("download limit reached")
        source = await download.path()
        if source is None:
            raise RuntimeError("download unavailable")
        source = Path(source)
        size = source.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            oversized = True
            raise RuntimeError("download too large")
        with source.open("rb") as handle:
            if handle.read(len(_PDF_MAGIC)) != _PDF_MAGIC:
                outcome.update(code="UNSUPPORTED_DOWNLOAD",
                               message="Only PDF downloads can be imported. This file was "
                                       "not a PDF.")
                raise RuntimeError("unsupported download")
        shutil.copyfile(source, destination)
        _write_json(folder / f"{download_id}.json", {
            "id": download_id, "mimeType": "application/pdf", "size": size,
            "name": name if name.lower().endswith(".pdf") else f"{name}.pdf"})
        published = True
        outcome_path.unlink(missing_ok=True)
    except Exception as err:  # noqa: BLE001 - every failure becomes a record, not a crash
        if published:
            raise                                # published files stay available
        logger.info("download not kept: %s", err)
        if oversized:
            outcome.update(code="DOWNLOAD_TOO_LARGE",
                           message="The download is over 10 MB. Choose a smaller PDF.")
        outcome["status"] = "failed"
        _write_json(outcome_path, outcome)
        destination.unlink(missing_ok=True)
        for stale in read_failures(directory)[MAX_FAILURES:]:
            (outcomes / f"{stale['id']}.json").unlink(missing_ok=True)
    finally:
        monitor.cancel()
        try:
            await download.delete()
        except Exception:  # noqa: BLE001
            pass
