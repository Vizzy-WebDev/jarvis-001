"""What has actually been spent — the conversational way in to cost tracking.

Deliberately a tool and not a screen, per the owner's own choice: the data and
tool layer first, and a dashboard only once they have used this and know what
they actually want to see.

Answers in three separate parts, and says plainly which of them is missing.
"How much have I spent" with an unpriced model in the roster has no single
honest number, and this returns the shape that admits that rather than one that
hides it.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk

PERIODS = ("today", "month", "week")


def _run(period: str = "month") -> dict[str, Any]:
    from ..cost import report

    wanted = (period or "month").strip().lower()
    if wanted == "today":
        data = report.today()
    elif wanted == "week":
        data = report.last_days(7)
    else:
        wanted, data = "month", report.month_to_date()

    groups = data["measured"]["groups"]
    if not groups:
        return {"ok": True, "period": wanted, "measured": [], "calculated": None,
                "note": "Nothing has been recorded for this period yet."}

    top = max(groups, key=lambda g: g["calls"])
    priceless = data["pricelessGroups"]
    notes = []
    if data["calculated"] is None:
        notes.append("No price is on record for anything used in this period, so there is "
                     "no money figure at all — only real usage counts.")
    elif priceless:
        names = ", ".join(f"{g['modelId'] or g['provider']}" for g in priceless[:5])
        notes.append(f"The money figure covers only what has a price on record. "
                     f"Usage with no known price ({names}) is counted but not costed — "
                     f"say so rather than presenting the total as complete.")

    return {
        "ok": True,
        "period": wanted,
        "since": data["since"],
        "calls": data["eventCount"],
        "measured": groups,
        "calculated": data["calculated"],
        "providerReported": data["providerReported"],
        "pricelessGroups": priceless,
        "mostUsed": {"modelId": top["modelId"], "provider": top["provider"],
                     "calls": top["calls"]},
        "note": " ".join(notes) or "Every number here was counted, not estimated.",
    }


SPEC = CapabilitySpec(
    id="builtin.check_spending",
    name="check_spending",
    description=("Check real recorded usage and spend across every paid service — tokens, "
                 "characters, seconds — for today, this week, or this month. Use it for any "
                 "question about cost, spending, usage or which model gets used most. Some "
                 "usage has no price on record and therefore no money figure: say that "
                 "plainly rather than presenting the total as if it were complete."),
    input_schema={"type": "object", "properties": {
        "period": {"type": "string", "description": '"today", "week" or "month".'}},
        "required": []},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=15.0,
    tags=frozenset({"core", "meta"}),
)
