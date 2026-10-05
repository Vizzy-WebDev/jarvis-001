"""Did the sentence actually use the number it was given?

A self-check retrieves real data and a model then writes a sentence on top of it.
Nothing checked whether the sentence used that data faithfully — this closes it
for exactly one narrow, machine-checkable slice: NUMBERS.

Free-form prose is `unverifiable` on purpose and never guessed at. Whether a
sentence's MEANING matches its source is not solvable this way, and pretending
otherwise would be the same false confidence the self-model exists to avoid.

**The bug this deliberately does not reproduce:** matching by substring made a
citable value of `0` match inside the text `"100%"`. Matching is word-boundary
safe on digits, and tested in both directions.
"""

from __future__ import annotations

import re
from typing import Any

USED = "used"
IGNORED = "ignored"
UNVERIFIABLE = "unverifiable"


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", (value or "").strip()))


def mentions_number(text: str, value: str) -> bool:
    """True when `value` appears in `text` as its own number.

    `(?<!\\d)` / `(?!\\d)` are what stop 0 matching inside 100, and a trailing
    `.0` is tolerated because "2" and "2.0" are the same claim.
    """
    number = (value or "").strip()
    if not _is_number(number):
        return False
    pattern = re.escape(number.rstrip("0").rstrip(".") if "." in number else number)
    return bool(re.search(rf"(?<![\d.]){pattern}(\.0+)?(?![\d.])", text or ""))


def verify_citation(snapshot_id: str, field_name: str, reply_text: str) -> dict[str, Any]:
    """Was this field's value actually used in the reply?

    The snapshot is re-read fresh rather than trusting the citation row's stored
    copy: the point is to check against what was really available, and a stored
    value could have been written by the same mistake being checked for.
    """
    from . import store

    snapshot = store.get_snapshot(snapshot_id)
    if snapshot is None:
        return {"verdict": UNVERIFIABLE, "reason": "that self-check is no longer on record"}

    citations = {c["field_name"]: c["field_value"] for c in store.list_citations(snapshot_id)}
    if field_name not in citations:
        return {"verdict": UNVERIFIABLE,
                "reason": "that isn't a number from the check — only numbers can be checked"}

    value = citations[field_name]
    if not _is_number(value):
        return {"verdict": UNVERIFIABLE, "reason": "not a numeric value"}
    return {"verdict": USED if mentions_number(reply_text, value) else IGNORED,
            "field": field_name, "value": value}
