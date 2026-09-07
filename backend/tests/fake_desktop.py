"""A desktop that isn't there, so the real code above it can be tested anyway.

The same technique as `stub_openai_server.py`: everything ABOVE the boundary is
the real implementation — the real tools, the real control loop, the real
guard — and only the last inch, the one that would touch a mouse, is scripted.

It records every call in order. Most of what matters about driving a desktop is
the SEQUENCE (focus before typing, a glide before a click, only closing what we
opened), and a sequence is exactly what an assertion can read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jarvis.control.desktop import Element, Shot, Window

#: A one-pixel PNG. Real bytes, so anything that decodes or serves it works.
#: Generated with a real encoder and checked chunk by chunk (signature, IHDR,
#: IDAT, IEND, every CRC) — a hand-typed one was wrong on the first attempt, and
#: a fake capture that isn't a real image makes every test built on it a lie.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c63f8cfc0f01f0005000"
    "1ff89993d1d0000000049454e44ae426082"
)


@dataclass
class FakeDesktop:
    windows_list: list[Window] = field(default_factory=list)
    elements: dict[str, list[Element]] = field(default_factory=dict)
    process_list: list[str] = field(default_factory=list)
    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)
    cursor_at: tuple[int, int] = (0, 0)
    #: Set to raise from the next call of that name — a real desktop fails too.
    fail_on: dict[str, Exception] = field(default_factory=dict)
    closed: list[str] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)
    typed: list[str] = field(default_factory=list)

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        failure = self.fail_on.get(name)
        if failure is not None:
            raise failure

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    # --- the protocol --------------------------------------------------------

    def windows(self) -> list[Window]:
        self._record("windows")
        return list(self.windows_list)

    def processes(self) -> list[str]:
        self._record("processes")
        return list(self.process_list)

    def focus(self, handle: str) -> None:
        self._record("focus", handle)

    def read_window(self, handle: str) -> list[Element]:
        self._record("read_window", handle)
        return list(self.elements.get(handle, []))

    def screenshot(self, handle: str | None = None) -> Shot:
        self._record("screenshot", handle)
        return Shot(data=PNG_BYTES, mime_type="image/png", width=1, height=1)

    def cursor(self) -> tuple[int, int]:
        self._record("cursor")
        return self.cursor_at

    def move_cursor(self, x: int, y: int) -> None:
        self._record("move_cursor", x, y)
        self.cursor_at = (x, y)

    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
        self._record("click", x, y, button=button, double=double)
        self.cursor_at = (x, y)

    def type_text(self, text: str) -> None:
        self._record("type_text", text)
        self.typed.append(text)

    def key(self, combo: str) -> None:
        self._record("key", combo)

    def scroll(self, amount: int) -> None:
        self._record("scroll", amount)

    def launch(self, app: str) -> None:
        self._record("launch", app)
        self.launched.append(app)

    def minimize(self, handle: str) -> None:
        self._record("minimize", handle)

    def restore(self, handle: str) -> None:
        self._record("restore", handle)

    def arrange(self, handle: str, x: int, y: int, width: int, height: int) -> None:
        self._record("arrange", handle, x, y, width, height)

    def close(self, handle: str) -> None:
        self._record("close", handle)
        self.closed.append(handle)
        self.windows_list = [w for w in self.windows_list if w.handle != handle]


def window(handle: str, title: str, process: str = "notepad", *,
           foreground: bool = False) -> Window:
    return Window(handle=handle, title=title, process_name=process,
                  foreground=foreground, rect=(0, 0, 800, 600))


def element(id_: int, role: str = "Button", name: str = "OK", value: str = "",
            center: tuple[int, int] = (100, 100)) -> Element:
    return Element(id=id_, role=role, name=name, value=value, center=center)
