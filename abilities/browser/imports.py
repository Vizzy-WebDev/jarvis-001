"""Turning a PDF the browser caught into a file Jarvis keeps.

Capture is the browser's (`downloads.py`); keeping is Jarvis's, so this is the one
place the browser meets the artifact store. A kept PDF is an ordinary artifact: it is
listed on the Artifacts page, opens in the viewer, belongs to the conversation that
asked for it, and only the person deletes it.

**Importing the same download twice returns the same artifact.** The browser
remembers which download became which artifact (`imported.json`), so a model that
calls the tool again, a person who presses the button after Jarvis already did, or a
retry after a crash cannot make two copies.

The artifact store's own check is a test of Jarvis's writers and is skipped here
(`keep(check=False)`): it would discard an ordinary downloaded PDF for the way another
program laid it out. What was verified is recorded honestly instead - that it began
`%PDF-` and was within the size limit, nothing more.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from ..artifacts import get, keep, safe_title, staging_path
from .errors import BrowserError
from .host import BrowserHost

logger = logging.getLogger(__name__)


def import_downloads(host: BrowserHost, session_id: str, *,
                     conversation_session: str | None = None) -> dict[str, Any]:
    """Keep every PDF the window has caught that is not already kept.

    Returns `{"files": [...], "created": [...], "failures": [...]}`: every artifact the
    window's downloads have become (earlier ones included, so the answer is the same
    however many times it is asked), which of those were made just now, and the
    downloads the browser refused.
    """
    caught = host.downloads(session_id)
    files: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []

    for download_id, artifact_id in caught["imported"].items():
        existing = get(artifact_id)
        if existing is not None and existing.path.exists():
            files.append(existing.as_result())

    for download in caught["downloads"]:
        try:
            source = Path(host.download_file(session_id, download["id"]))
        except BrowserError:
            continue                                        # it went away between the calls
        staging = staging_path(download["name"])
        try:
            shutil.copyfile(source, staging)
            artifact = keep(staging, name=download["name"], session_id=conversation_session,
                            title=safe_title(Path(download["name"]).stem), check=False)
        except (OSError, ValueError) as err:
            logger.info("could not keep download %s: %s", download["id"], err)
            continue
        host.mark_imported(session_id, download["id"], artifact.id)
        files.append(artifact.as_result())
        created.append(artifact.as_result())
    return {"files": files, "created": created, "failures": caught["failures"]}
