"""What kind of file something is, and how to put a small one in front of a model.

Shared by attachments, content analysis and the web reader, so none of them has
to import a feature module to answer "is this a video". A leaf: the filesystem
and the adapter capability table, nothing else.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

#: Biggest file to base64 into a request body rather than uploading. Base64
#: inflates by 4/3 and Anthropic caps an image at 5MB encoded, so ~3.5MB of raw
#: bytes is the largest that is safe across every adapter.
INLINE_MAX_BYTES = int(3.5 * 1024 * 1024)

VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg",
                            ".mpeg", ".wmv", ".3gp", ".mts", ".m2ts"})
AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac",
                            ".wma", ".amr", ".m4b"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
DOCUMENT_SUFFIXES = frozenset({".pdf", ".txt", ".md", ".csv", ".json", ".rtf"})

MIME_BY_SUFFIX: dict[str, str] = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".avi": "video/x-msvideo", ".m4v": "video/x-m4v",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".wmv": "video/x-ms-wmv",
    ".3gp": "video/3gpp", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".opus": "audio/opus",
    ".flac": "audio/flac", ".wma": "audio/x-ms-wma", ".amr": "audio/amr",
    ".m4b": "audio/mp4",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".csv": "text/csv", ".json": "application/json", ".rtf": "application/rtf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def mime_type_for(path: Path | str) -> str:
    return MIME_BY_SUFFIX.get(Path(path).suffix.lower(), "application/octet-stream")


def file_kind(path: Path | str) -> str:
    """'video' | 'audio' | 'image' | 'document' | 'unknown'.

    Decides whether a model has to WATCH something or can simply read it, which
    is the difference between a capability requirement and no requirement at all.
    """
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    return "unknown"


def human_size(size: int | None) -> str | None:
    if not size:
        return None
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB"


class TooBig(ValueError):
    """The file is real and readable, just past the inline ceiling."""

    def __init__(self, size: int) -> None:
        super().__init__(f"{size} bytes is past the inline limit")
        self.size = size


def inline_attachment(path: Path | str, mime_type: str | None = None, *,
                      max_bytes: int = INLINE_MAX_BYTES) -> list[dict[str, Any]]:
    """One neutral media item carrying the file's own bytes.

    This is the path anything small enough should take, and it needs NO provider
    upload API: all three adapters already understand base64 media. Requiring an
    upload for an image — which only one adapter implements — is what made
    attaching a picture fail on a model that could have displayed it perfectly.
    """
    target = Path(path)
    size = target.stat().st_size
    if size > max_bytes:
        raise TooBig(size)
    kind = "image" if file_kind(target) == "image" else "document"
    return [{"kind": kind, "mimeType": mime_type or mime_type_for(target),
             "dataBase64": base64.b64encode(target.read_bytes()).decode("ascii")}]


# --- is this actually text, whatever it is called ----------------------------

SNIFF_BYTES = 8000
#: Above this share of replacement characters, the decode is guessing.
MAX_REPLACEMENT_RATIO = 0.02


def read_as_text(path: Path | str) -> str | None:
    """The real bytes, decoded, or None if this is not text.

    Reading the bytes and asking "does this decode" covers every code, config and
    log format at once — the alternative is an extension list that is never
    finished, and a `.kt` file nobody thought of reading as binary.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None

    sample = data[:SNIFF_BYTES]
    if b"\x00" in sample and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None                      # a NUL byte this early means binary

    for bom, encoding in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            try:
                return data[len(bom):].decode(encoding)
            except UnicodeDecodeError:
                return None

    decoded = data.decode("utf-8", errors="replace")
    if decoded and decoded.count("�") / len(decoded) > MAX_REPLACEMENT_RATIO:
        return None
    return decoded
