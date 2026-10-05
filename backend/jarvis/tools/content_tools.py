"""Content the user hands over: registering it, asking about it, checking a claim.

The three of these are deliberately separate, and the separation is the design:
registering costs nothing and reads nothing, asking is what triggers real work,
and checking whether something is TRUE researches independently rather than
answering from the content itself.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..capabilities import CapabilitySpec, Risk


def _share(source: str = "", instruction: str = "") -> dict[str, Any]:
    from ..content import store
    from ..content.intake import classify_source, describe
    from ..content.investigator import share
    from ..session import get_active_session_id

    raw = (source or "").strip()
    if not raw:
        return {"ok": False, "error": "I didn't catch what you wanted me to look at."}

    session_id = get_active_session_id()
    shared = share(source=classify_source(raw), session_id=session_id,
                   instruction=(instruction or "").strip() or None)
    record, identity = shared["record"], shared["identity"]

    if identity.get("unreachable"):
        return {"ok": False, "contentId": record["id"], "error": identity["unreachable"]}

    answer: dict[str, Any] = {
        "ok": True, "contentId": record["id"], "what": describe(identity),
        "kind": identity.get("kind"), "title": identity.get("title"),
    }
    if (instruction or "").strip():
        answer["spoken_hint"] = ("Say you're looking into it now. The answer arrives in the "
                                 "conversation shortly — don't invent one here.")
    else:
        answer["spoken_hint"] = ("Say what it appears to be and ask what they want done with "
                                 "it. Nothing has been read or watched yet — do not summarise "
                                 "or guess at the contents.")
        answer["note"] = ("Registered only. Nothing has been read, watched or listened to. "
                          "Use examine_content once they say what they want.")
    return answer


def _examine(request: str = "", content_id: str = "") -> dict[str, Any]:
    from ..content import store
    from ..content.investigator import examine
    from ..session import get_active_session_id

    text = (request or "").strip()
    if not text:
        return {"ok": False, "error": "I didn't catch what you wanted to know."}

    session_id = get_active_session_id()
    record = (store.get_content(content_id) if content_id else None) \
        or store.latest_in_session(session_id)
    if record is None:
        return {"ok": False,
                "error": "There's nothing I've been shown recently. Share a link, a file or "
                         "some text first."}

    examine(record["id"], text, session_id=session_id)
    return {"ok": True, "contentId": record["id"],
            "about": (record.get("identity") or {}).get("title") or "that",
            "spoken_hint": ("Say you're looking into it. The answer, and how it was actually "
                            "taken in, appear in the conversation when ready — give the gist "
                            "then, not the whole thing.")}


def _check_claim(claim: str = "", realism: bool = False,
                 context: str = "") -> dict[str, Any]:
    from ..content.investigator import judge_claim
    from ..events import EventType, bus

    text = (claim or "").strip()
    if not text:
        return {"ok": False, "error": "I didn't catch which claim you wanted checked."}

    judged = judge_claim(text, context=(context or "").strip() or None,
                         realism=bool(realism))
    if not judged["ok"]:
        return judged

    # The full write-up is a document, and the tool-result channel back to the
    # model carries only a few fields — so the write-up is published as its own
    # event, or the document card the spoken hint promises would not exist.
    bus.publish(EventType.JOB_UPDATED, {
        "kind": "content", "id": f"cc{uuid.uuid4().hex[:10]}",
        "title": f"Checked: \"{text[:60]}…\"" if len(text) > 60 else f"Checked: \"{text}\"",
        "status": "ready", "document": judged["result"], "sources": judged["sources"],
        "verdict": judged["verdict"]})

    return {"ok": True, "verdict": judged["verdict"], "researched": judged["researched"],
            "sources": judged["sources"],
            "spoken_hint": (f"The verdict is \"{judged['verdict']}\". Say that and the one "
                            f"thing that decides it. The full write-up is already in the "
                            f"conversation — don't read it out.")}


SPECS = [
    CapabilitySpec(
        id="builtin.share_content", name="share_content",
        description=("Register something the user shares — a YouTube link, a web article, a "
                     "file on their computer, or text they paste or dictate. Use it whenever "
                     "they hand you something to look at, whether or not they said what they "
                     "want done with it. This only works out what the thing IS; it does not "
                     "read or watch it. If they DID say what they want in the same breath, "
                     "pass that as instruction and it is looked into right away. If they did "
                     "not, leave it out — asking them is correct, not a failure."),
        input_schema={"type": "object", "properties": {
            "source": {"type": "string",
                       "description": ("What they gave you: a URL, a file path, or the text "
                                       "itself. Pass it through exactly as they said it — "
                                       "never tidy up or shorten a link.")},
            "instruction": {"type": "string",
                            "description": ("What they want done with it, in their own words, "
                                            "ONLY if they already said. Leave out otherwise; "
                                            "never invent one.")}},
            "required": ["source"]},
        risk=Risk.LOW, handler=_share, timeout_s=45.0, tags=frozenset({"core", "meta"}),
    ),
    CapabilitySpec(
        id="builtin.examine_content", name="examine_content",
        description=("Ask something about content already registered — what it says, what "
                     "happens in it, what it means, whether it is worth the time. Use it for "
                     "any follow-up about shared content, and whenever you asked what they "
                     "wanted done with something and they answered. Do NOT use it to check "
                     "whether a claim is true — that is check_claim."),
        input_schema={"type": "object", "properties": {
            "request": {"type": "string",
                        "description": "What they want to know, in their own words."},
            "content_id": {"type": "string",
                           "description": ("Which piece of content. Use the reference id "
                                           "noted when it was registered; leave out only if "
                                           "there is genuinely just one thing in play.")}},
            "required": ["request"]},
        risk=Risk.LOW, handler=_examine, timeout_s=20.0, tags=frozenset({"core", "meta"}),
    ),
    CapabilitySpec(
        id="builtin.check_claim", name="check_claim",
        description=("Check whether a specific claim is actually true, or whether something "
                     "described is realistic rather than exaggerated. Looks it up "
                     "independently before judging, and answers with one of: checks out, "
                     "partly true, misleading, false, or \"can't tell\". Prefer it over "
                     "answering from your own knowledge whenever the truth of a claim is the "
                     "actual question."),
        input_schema={"type": "object", "properties": {
            "claim": {"type": "string",
                      "description": ("The single claim, stated plainly on its own — \"you "
                                      "can make $12,000 in your first month\", not \"is that "
                                      "video right\". Quote figures exactly: they are what "
                                      "gets looked up.")},
            "realism": {"type": "boolean",
                        "description": ("True when the question is \"is this realistic or "
                                        "typical\" rather than \"is this factually true\".")},
            "context": {"type": "string",
                        "description": "Optional: where it came from, what they are deciding."}},
            "required": ["claim"]},
        risk=Risk.LOW, handler=_check_claim, timeout_s=120.0, tags=frozenset({"meta"}),
    ),
]
