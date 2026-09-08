"""Getting JSON out of a model that was asked for JSON.

Every provider here is asked the same way — in words — rather than through a
per-provider structured-output feature, because only one of the three has one and
a capability that exists for a third of the roster is not something the rest of
the system can rely on.

So the parsing has to tolerate what models actually do: a ```json fence, a
sentence of preamble, a trailing "Let me know if you'd like...". It does NOT try
to repair malformed JSON — a half-parsed object is worse than a clean failure,
because the caller cannot tell which fields were real.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json(text: str) -> Any | None:
    """The first well-formed JSON value in `text`, or None. Never raises."""
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
