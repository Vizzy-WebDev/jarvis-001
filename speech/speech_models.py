"""The on-device speech models: fetched when first needed, with progress, never bundled.

Offline recognition (Moonshine, with the Silero speech detector) and offline voices (Piper,
Kokoro) run through sherpa-onnx from model files that are far too big to ship with Jarvis
(30-320 MB each). Each is downloaded once, on demand — when the person chooses something that
needs it — into `data/speech/<id>/`, and is ready from then on with no network at all.

**Progress is real and readable at any moment** (`status()`): bytes received of the size the
server announced, so a screen can show a bar instead of a spinner. A download goes to a
`.part` file and is unpacked into a temporary folder that is renamed into place only once
every file the model needs is there, so a cut-off download is never mistaken for a model.

**Fetching reaches the network, so it is an interlock** (`DOWNLOAD_ENV`), like the wake
word's: off unless `main()` (the real launch) turns it on. A test never downloads anything.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DOWNLOAD_ENV = "JARVIS_SPEECH_MODEL_DOWNLOAD"
_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class SpeechModel:
    id: str
    label: str
    url: str
    #: Bytes, as published — for the progress bar before the server says.
    size: int
    #: Files that must exist (relative to the model's folder) for it to count as there.
    needs: tuple[str, ...]
    #: A single file rather than an archive (the speech detector).
    single_file: bool = False


CATALOG: dict[str, SpeechModel] = {m.id: m for m in (
    SpeechModel("silero-vad", "Speech detector", f"{_RELEASES}/asr-models/silero_vad.onnx",
                643_854, ("silero_vad.onnx",), single_file=True),
    SpeechModel("moonshine-base-en", "Moonshine Base (English)",
                f"{_RELEASES}/asr-models/sherpa-onnx-moonshine-base-en-quantized-2026-02-27.tar.bz2",
                111_266_225, ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt")),
    SpeechModel("moonshine-tiny-en", "Moonshine Tiny (English)",
                f"{_RELEASES}/asr-models/sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27.tar.bz2",
                29_858_559, ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt")),
    SpeechModel("piper-lessac-medium", "Piper · Lessac",
                f"{_RELEASES}/tts-models/vits-piper-en_US-lessac-medium.tar.bz2",
                67_230_653, ("en_US-lessac-medium.onnx", "tokens.txt", "espeak-ng-data")),
    SpeechModel("kokoro-en", "Kokoro (English)",
                f"{_RELEASES}/tts-models/kokoro-en-v0_19.tar.bz2",
                319_625_534, ("model.onnx", "voices.bin", "tokens.txt", "espeak-ng-data")),
)}


@dataclass
class _Progress:
    state: str = "missing"        # missing | downloading | unpacking | ready | failed
    done: int = 0
    total: int = 0
    error: str | None = None
    started: float = 0.0
    thread: threading.Thread | None = field(default=None, repr=False)


_lock = threading.Lock()
_progress: dict[str, _Progress] = {}


def root() -> Path:
    from .store import data_dir
    return data_dir() / "speech"


def folder(model_id: str) -> Path:
    return root() / model_id


def is_ready(model_id: str) -> bool:
    model = CATALOG[model_id]
    base = folder(model_id)
    return all((base / name).exists() for name in model.needs)


def path(model_id: str, name: str) -> str:
    return str(folder(model_id) / name)


def status(model_id: str) -> dict[str, Any]:
    """Where a model is, in a shape a screen can draw: state, bytes so far, bytes in all."""
    model = CATALOG[model_id]
    with _lock:
        progress = _progress.get(model_id)
        if is_ready(model_id):
            return {"id": model_id, "label": model.label, "state": "ready",
                    "done": model.size, "total": model.size, "error": None}
        if progress is None:
            return {"id": model_id, "label": model.label, "state": "missing",
                    "done": 0, "total": model.size, "error": None}
        return {"id": model_id, "label": model.label, "state": progress.state,
                "done": progress.done, "total": progress.total or model.size,
                "error": progress.error}


def ensure(model_id: str) -> dict[str, Any]:
    """Start fetching the model if it is not here and not already on its way. Returns at
    once with its status; never waits for a download."""
    if is_ready(model_id):
        return status(model_id)
    with _lock:
        progress = _progress.get(model_id)
        busy = progress is not None and progress.thread is not None and progress.thread.is_alive()
        if not busy:
            if os.environ.get(DOWNLOAD_ENV) != "1":
                _progress[model_id] = _Progress(state="failed", error=(
                    "Jarvis is not allowed to download speech models here."))
            else:
                progress = _Progress(state="downloading", started=time.time())
                progress.thread = threading.Thread(target=_fetch, args=(model_id, progress),
                                                   name=f"speech-model-{model_id}", daemon=True)
                _progress[model_id] = progress
                progress.thread.start()
    return status(model_id)


def _fetch(model_id: str, progress: _Progress) -> None:
    import httpx

    from . import tls

    model = CATALOG[model_id]
    base = folder(model_id)
    root().mkdir(parents=True, exist_ok=True)
    part = root() / f"{model_id}.part"
    staging = root() / f"{model_id}.unpacking"
    try:
        with httpx.stream("GET", model.url, follow_redirects=True, timeout=60,
                          verify=tls.context()) as reply:
            reply.raise_for_status()
            progress.total = int(reply.headers.get("content-length") or model.size)
            with open(part, "wb") as out:
                for chunk in reply.iter_bytes(1 << 16):
                    out.write(chunk)
                    progress.done += len(chunk)
        progress.state = "unpacking"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        if model.single_file:
            os.replace(part, staging / model.needs[0])
        else:
            with tarfile.open(part, "r:bz2") as archive:
                archive.extractall(staging, filter="data")
            part.unlink(missing_ok=True)
            # The archive holds one top folder; its contents are the model.
            inner = [p for p in staging.iterdir() if p.is_dir()]
            if len(inner) == 1 and not any((staging / n).exists() for n in model.needs):
                for item in inner[0].iterdir():
                    shutil.move(str(item), staging / item.name)
                inner[0].rmdir()
        missing = [n for n in model.needs if not (staging / n).exists()]
        if missing:
            raise RuntimeError(f"the download is missing {', '.join(missing)}")
        shutil.rmtree(base, ignore_errors=True)
        os.replace(staging, base)
        progress.state = "ready"
    except Exception as err:  # noqa: BLE001 — reported to the screen, not raised
        logger.warning("speech model %s could not be fetched: %s", model_id, err)
        progress.state = "failed"
        progress.error = ("The download did not finish. Check the internet connection and try again."
                          if isinstance(err, (httpx.HTTPError, OSError)) else str(err))
        part.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def reset_for_tests() -> None:
    with _lock:
        _progress.clear()
