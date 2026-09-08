"""Recording the screen to a real, playable video.

ffmpeg does the work. It is not bundled and never becomes a dependency of this
project — it is resolved on PATH and at the usual install locations, and when it
is genuinely not there the answer is a plain-language explanation of what it is
and how to add it, not a raw error about a missing executable.

Two details that decide whether the file is playable at all:

* **`libx264` + `yuv420p` + `+faststart`.** Raw `gdigrab` output is not
  something a browser's `<video>` element will play; this combination is.
* **Stopping means writing `q` to ffmpeg's own stdin**, the convention it
  documents for a clean stop, so the container is finalised. Killing the process
  leaves a file that exists, has a plausible size, and does not play — the worst
  kind of failure, because it looks like success.

Independent of the control loop on purpose: a recording keeps running through a
control session, which is the whole point of being able to record one.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..childenv import scrubbed_environment
from .captures import RECORDING, get, reserve

logger = logging.getLogger(__name__)

CANDIDATES = (
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
)

FRAMERATE = "12"
#: How long to let ffmpeg finalise the file after being asked to stop, before
#: force-killing. Generous: a truncated container is worse than a slow stop.
STOP_TIMEOUT_S = 15.0

NO_FFMPEG = (
    "Screen recording needs a free, independent program called ffmpeg, which isn't "
    "installed. To add it: go to ffmpeg.org/download.html, download the Windows build, "
    "unzip it anywhere (for example C:\\ffmpeg), then add its \\bin folder to your PATH "
    "(Windows Settings > System > About > Advanced system settings > Environment "
    "Variables). Then ask me again."
)

_ffmpeg_path: str | None = None


def find_ffmpeg() -> str | None:
    """A usable ffmpeg, or None. Resolved once — a real install does not move
    mid-session, and probing per call puts a process launch in front of every
    recording."""
    global _ffmpeg_path
    if _ffmpeg_path is not None:
        return _ffmpeg_path or None
    found = shutil.which("ffmpeg")
    if not found:
        for candidate in CANDIDATES:
            if Path(candidate).exists():
                found = candidate
                break
    _ffmpeg_path = found or ""
    return _ffmpeg_path or None


@dataclass
class Recording:
    id: str
    path: Path
    process: Any
    started_at: float


_active: Recording | None = None


def is_recording() -> bool:
    return _active is not None and _active.process.poll() is None


def start(*, spawn: Any = None) -> dict[str, Any]:
    global _active
    if is_recording():
        return {"ok": False, "error": "I'm already recording your screen."}

    # Whether ffmpeg is here is asked first and independently of how it will be
    # launched: an injected launcher is a caller's own business, and letting it
    # decide what "installed" means is how a test starts proving nothing.
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return {"ok": False, "error": NO_FFMPEG}
    if sys.platform != "win32" and spawn is None:
        # `gdigrab` is the Windows desktop capture device; ffmpeg elsewhere is
        # real, but it has no screen here to point at.
        return {"ok": False,
                "error": "I can only record the screen on Windows, and this isn't Windows."}

    capture_id, path = reserve(RECORDING)
    args = [ffmpeg, "-f", "gdigrab", "-framerate", FRAMERATE, "-i", "desktop",
            "-vcodec", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-y", str(path)]
    launch = spawn or _spawn
    try:
        process = launch(args)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f"I couldn't start recording: {err}"}

    _active = Recording(id=capture_id, path=path, process=process, started_at=time.time())
    return {"ok": True, "id": capture_id}


def _spawn(args: list[str]) -> Any:
    # stdin stays a real pipe: that is how a clean stop is asked for. stdout and
    # stderr are discarded — ffmpeg's progress chatter is not useful here, and
    # buffering it for a long recording would grow without bound.
    return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=scrubbed_environment())


def stop() -> dict[str, Any]:
    global _active
    recording, _active = _active, None
    if recording is None:
        return {"ok": False, "error": "Nothing is being recorded right now."}

    try:
        if recording.process.stdin is not None:
            recording.process.stdin.write(b"q")
            recording.process.stdin.flush()
            recording.process.stdin.close()
    except Exception as err:  # noqa: BLE001
        logger.info("could not ask ffmpeg to stop cleanly: %s", err)

    try:
        recording.process.wait(timeout=STOP_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        # Only after asking properly and waiting: a killed ffmpeg leaves a file
        # that exists and does not play.
        logger.info("ffmpeg did not stop on its own; ending it")
        try:
            recording.process.kill()
        except Exception:  # noqa: BLE001
            pass

    seconds = round(time.time() - recording.started_at, 1)
    capture = get(RECORDING.name, recording.id)
    if capture is None or capture.size == 0:
        return {"ok": False,
                "error": "The recording stopped but didn't leave a usable file."}
    return {"ok": True, "id": capture.id, "seconds": seconds,
            "bytes": capture.size, "url": capture.as_dict()["url"]}


def reset_for_tests() -> None:
    global _active, _ffmpeg_path
    _active = None
    _ffmpeg_path = None
