"""Real media for Content Management tests — no Pillow, no ffmpeg.

`png()` writes a genuine, decodable PNG with the standard library alone, so a
browser test can assert that an <img> actually decoded it (naturalWidth > 0)
rather than that a tag merely exists.
"""

from __future__ import annotations

import struct
import zlib


def png(width: int = 64, height: int = 36, rgb: tuple[int, int, int] = (40, 120, 220)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))
