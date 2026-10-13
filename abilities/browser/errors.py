"""One error type for everything the browser refuses or cannot do.

Ported from OpenMuse's `WorkerError` (apps/worker/src/errors.ts, MIT): a short
machine code, a sentence a person can read, and the HTTP status a route should
answer with. The message is what reaches the model and the screen, so it is
written in plain words and never carries a stack trace or an internal path.
"""

from __future__ import annotations


class BrowserError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}
