"""Getting JSON out of a model that was asked for it in words.

Structured output (§22, `ai/request.py`'s `ResponseFormat`) is the real,
schema-enforced path where a model declares support for it. This is the
fallback every model can be asked through regardless: a plain instruction to
answer in JSON, tolerating what models actually do with that — a ```json
fence, a sentence of preamble, a trailing "let me know if you'd like...". It
does NOT try to repair malformed JSON — a half-parsed object is worse than a
clean failure, because a caller cannot tell which fields were real.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json(text: str) -> Any | None:
    """The first well-formed JSON value in `text`, or `None`. Never raises."""
    if not text:
        return None
    for candidate in _candidates(text):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _candidates(text: str) -> list[str]:
    out = [text.strip()]
    fenced = _FENCE.search(text)
    if fenced:
        out.append(fenced.group(1).strip())
    # The outermost brace/bracket span — what is left when a model wraps real
    # JSON in prose on both sides.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            out.append(text[start:end + 1])
    return out
