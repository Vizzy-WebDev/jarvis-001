"""Taking secrets back out of text before it is stored, logged, or shown.

A provider's raw error can echo the request that caused it — including the key
that was sent. That text has three destinations here, and all three are places a
key must never reach: `data/model-availability.json` (a file, kept until the
model next succeeds), the application log (§25: never log secrets), and the
model itself, when a connector's failure is handed back as a tool result and
persisted in the conversation.

Two lines of defence, because neither is sufficient alone:

* **The values actually held.** `config.secret_values()` knows what was saved,
  which is the only way to catch a self-hosted gateway's key — it has no fixed
  shape at all.
* **The well-known shapes.** `sk-…` (OpenAI/Anthropic) and `AIza…` (Google),
  which catch a key that was never saved here: typed into a form and submitted,
  or configured directly as an environment variable.

Ported from the Node build's `server/models/redact.js`, including its rule that
a stored value shorter than four characters is skipped — a trivially short
secret would otherwise blank out unrelated text wherever those characters
happen to appear.

Nothing is cached: this runs on failure paths only, and a cache is exactly what
would leave a key unredacted for the few seconds after it was saved — which is
when a wrong key is most likely to produce an error that echoes it.
"""

from __future__ import annotations

import re

from .config import secret_values

MASK = "••••"

#: A bare provider-shaped key wherever it appears in text.
KNOWN_KEY_SHAPE = re.compile(r"\b(sk-[a-zA-Z0-9_-]{10,}|AIza[a-zA-Z0-9_-]{10,})\b")

#: Below this, a value is too short to be replaced safely — see the docstring.
MIN_SECRET_LENGTH = 4


def redact(text: object, extra: object = ()) -> object:
    """Replace every known secret, and anything key-shaped, with `••••`.

    `extra` carries a key that has been submitted but not saved yet — testing a
    connection before it exists is exactly when a bad key produces an error that
    quotes it back.

    Anything that is not a non-empty string is returned unchanged, and this
    never raises: a redaction that fails must not swallow the error it was
    called to clean up.
    """
    if not text or not isinstance(text, str):
        return text
    out = text
    try:
        candidates = list(secret_values())
    except Exception:  # noqa: BLE001 — an unreadable .env must not lose the shape pass
        candidates = []
    if isinstance(extra, str):
        candidates.append(extra)
    else:
        try:
            candidates.extend(str(value) for value in extra)  # type: ignore[union-attr]
        except TypeError:
            pass
    for secret in candidates:
        if not secret or len(secret) < MIN_SECRET_LENGTH:
            continue
        out = out.replace(secret, MASK)
    return KNOWN_KEY_SHAPE.sub(MASK, out)


def redact_text(text: str | None, extra: object = ()) -> str | None:
    """`redact()` for a caller that has a string or None and wants one back."""
    result = redact(text, extra)
    return result if result is None or isinstance(result, str) else str(result)
