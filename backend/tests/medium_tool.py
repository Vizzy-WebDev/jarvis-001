"""A stand-in MEDIUM-risk capability, for tests about the approval flow itself.

Those tests used to borrow `create_artifact` as "something that must be asked",
until making a file the person asked for stopped needing a confirmation (it is
LOW: it stays in Jarvis's own folder and can be deleted). What they test is the
gate, not files — so they get a capability that exists only to be gated.
"""

from __future__ import annotations

from typing import Any

NAME = "save_to_shared_drive"


def install(agent_id: str | None = "content") -> str:
    """Register the capability in the live registry and, if `agent_id` is given,
    add it to that specialist's access. Returns its name."""
    from jarvis import assembly
    from jarvis.agents import store
    from jarvis.capabilities import CapabilitySpec, Risk

    registry = assembly.get_registry()
    if registry.get(NAME) is None:
        registry.register(CapabilitySpec(
            id=f"test.{NAME}", name=NAME,
            description="Save a file to the team's shared drive.",
            input_schema={"type": "object", "properties": {
                "filename": {"type": "string"}, "content": {"type": "string"}},
                "required": ["filename"]},
            risk=Risk.MEDIUM,
            handler=_save,
            summarize=lambda args: f'Save "{args.get("filename")}" to the shared drive?',
        ))
    if agent_id:
        agent = store.get_agent(agent_id)
        access = dict(agent["capabilityAccess"])
        access["names"] = [*(access.get("names") or []), NAME]
        store.update_agent(agent_id, {"capabilityAccess": access})
    return NAME


def _save(filename: str = "", content: str = "") -> dict[str, Any]:
    return {"ok": True, "saved": filename, "bytes": len(content or "")}
