"""A brief for whoever's coding assistant will actually do the work.

For a proposal that needs real code or a new Skill, this is what "approve"
produces instead of a change: Jarvis writes down what it wants and why, and a
person takes it to whichever assistant they use. The architecture summary here is
static and hand-written — it never reads the repository — which is the whole
reason this is safe to generate automatically.
"""

from __future__ import annotations

from typing import Any

from . import store

ARCHITECTURE = """Jarvis is a local personal assistant: a Python/FastAPI backend on
127.0.0.1 with a static front end served by the same process. Capabilities are
registered as specs (name, description, JSON-schema arguments, a risk level of
LOW/MEDIUM/HIGH, a timeout and a retry policy) and run through one executor that
owns timeouts, retries, idempotency and the permission gate. Anything MEDIUM or
HIGH is confirmed by the user before it runs. Data lives in SQLite plus a handful
of JSON files under a data directory."""


def build(proposal_id: str, target: str = "your coding assistant") -> dict[str, Any]:
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        raise KeyError(f"No such proposal: {proposal_id}")

    evidence = proposal.get("evidence") or []
    prompt = "\n\n".join([
        f"# {proposal['title']}",
        f"## What Jarvis noticed\n{proposal.get('rationale') or '(no rationale recorded)'}",
        f"## Evidence\nBased on {len(evidence)} recorded outcome(s) of its own work.",
        f"## Where this fits\n{ARCHITECTURE}",
        "## What to build\n"
        + str((proposal.get("payload") or {}).get("text") or proposal["title"]),
        "## Constraints\n"
        "- Keep the existing capability contract: a new ability is one spec with a "
        "risk level, a timeout and a JSON-schema argument list.\n"
        "- Anything that changes the user's data or reaches outside the machine is at "
        "least MEDIUM risk and must be confirmed.\n"
        "- No new dependencies unless there is no reasonable alternative.",
    ])
    store.set_proposal_implementation(proposal_id, prompt=prompt, target=target)
    return {"proposalId": proposal_id, "target": target, "prompt": prompt}
