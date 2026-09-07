"""Looking into shared content, on the user's own terms.

Two entry points, and nothing chains from one into the other:

* `share(source)` — the free glance, then stored. No model call.
* `examine(id, request)` — the one real "look into this" call, shaped by the
  question actually asked rather than by a fixed summary template.

Expensive media caches a neutral `observations` note the first time it is really
watched, so a second question does not re-send the bytes. But a follow-up the
notes cannot honestly answer says so and looks again for real, rather than
guessing from a stale note. Text sources are cheap to re-read and always are.

`judge_claim()` also lives here, with the one guarantee carried over unchanged:
research runs before every verdict, with no code path around it. When that lived
behind a wording heuristic the guarantee was really "a check is usually routed
correctly"; a function that always looks up before it judges cannot be skipped,
including by a model that thinks it already knows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .. import conversation
from ..background import run_in_background
from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..media import file_kind, mime_type_for, read_as_text
from ..research import research, to_search_query
from . import store
from .intake import (
    CannotPrepare, describe, fetch_youtube_text, identify, intake_description, prepare_for,
)

logger = logging.getLogger(__name__)

TEXT_FILE_MAX_CHARS = 40000

MEDIA_SYSTEM = """You are looking at a piece of content on the user's behalf, to answer a specific question they just asked. Answer THAT question — do not produce a general summary unless they actually asked for one.

Rules:
- Answer only from what is genuinely in the content. Never add outside facts or guess at what was not shown or said — looking things up is a separate ability with its own tool, not yours here.
- If answering properly would need outside information, say so plainly instead of inventing it.
- Be specific: quote or closely describe what was actually seen or heard where it matters.
- Neutral and factual unless the question itself asks you to judge something.

Reply as JSON:
{
  "answer": "the actual answer, in markdown",
  "observations": "a thorough, neutral note on what you actually perceived — broad enough that a DIFFERENT follow-up question could be answered from these notes alone, without looking again. Your own working notes, not a summary for the user."
}"""

CACHED_FOLLOWUP_SYSTEM = """You previously looked at a piece of content and took working notes. Answer the user's new question from those notes.

If the notes genuinely do not cover what is needed, do not guess — say so.

Reply as JSON:
{
  "answer": "the answer, if the notes cover it — otherwise a brief note on what is missing",
  "needsAnotherLook": true or false,
  "why": "one short sentence, only when needsAnotherLook is true"
}"""

TEXT_SYSTEM = """You are answering a specific question about content the user shared, using the text given to you.

Rules:
- Answer only from the text given, and any research note also given. If the answer genuinely is not in either, say so rather than filling it in.
- Answer the question actually asked — no general summary unless asked for one.
- Use markdown: this is read on screen, not spoken aloud."""


def _announce(record: dict[str, Any] | None, *, event_bus: EventBus | None = None,
              **extra: Any) -> None:
    if not record:
        return
    (event_bus or default_bus).publish(EventType.JOB_UPDATED, {
        "kind": "content", "id": record["id"],
        "title": (record.get("identity") or {}).get("title") or "that",
        "status": extra.pop("status", "working"), **extra})


# --- sharing: the free glance and nothing more --------------------------------

def share(*, source: dict[str, Any], session_id: str, instruction: str | None = None,
          event_bus: EventBus | None = None) -> dict[str, Any]:
    """Register content and work out what it is. No model call.

    `instruction`, when given, is examined right away — the one case where
    waiting to be asked would be pointless, because the user already said what
    they wanted in the same breath as sharing it.
    """
    found = identify(source)
    record = store.create_content(
        source=source, session_id=session_id,
        identity={"title": found.get("title"), "kind": found.get("kind"),
                  "detail": found.get("detail"), "thumbnail": found.get("thumbnail")})

    if found.get("cachedText") is not None:
        store.cache_material(record["id"], {
            "text": found["cachedText"],
            "intake": "article" if source.get("kind") == "url" else "text"})

    _announce(record, event_bus=event_bus, status="shared", description=describe(found),
              unreachable=found.get("unreachable"))

    asked = (instruction or "").strip()
    if asked:
        examine(record["id"], asked, session_id=session_id, event_bus=event_bus)

    return {"record": store.get_content(record["id"]), "identity": found}


# --- examining ----------------------------------------------------------------

def _is_text_shaped(source: dict[str, Any]) -> bool:
    """Content that is genuinely text: cheap and safe to re-read for every
    question, so it is never frozen into a one-shot observation."""
    kind = source.get("kind")
    if kind in ("url", "text"):
        return True
    if kind == "file":
        if file_kind(source["filePath"]) in ("video", "audio", "image"):
            return False
        return mime_type_for(source["filePath"]) != "application/pdf"
    return False


def _text_for(record: dict[str, Any]) -> dict[str, Any]:
    from ..webtext import fetch_article

    cached = (record.get("material") or {}).get("text")
    if cached:
        return {"ok": True, "text": cached,
                "intake": (record.get("material") or {}).get("intake") or "text"}

    source = record["source"]
    if source.get("kind") == "url":
        article = fetch_article(source["url"])
        if not article.ok:
            return {"ok": False, "error": article.error}
        store.cache_material(record["id"], {"text": article.text, "intake": "article"})
        return {"ok": True, "text": article.text, "intake": "article"}

    if source.get("kind") == "text":
        text = source.get("text") or ""
        store.cache_material(record["id"], {"text": text, "intake": "text"})
        return {"ok": True, "text": text, "intake": "text"}

    if source.get("kind") == "file":
        path = Path(source["filePath"])
        if path.suffix.lower() in (".docx", ".xlsx", ".pptx"):
            from ..documents import extract_document

            extracted = extract_document(path)
            if not extracted["ok"]:
                return {"ok": False, "error": extracted["error"]}
            store.cache_material(record["id"], {"text": extracted["markdown"],
                                                "intake": "document"})
            return {"ok": True, "text": extracted["markdown"], "intake": "document"}

        text = read_as_text(path)
        if text is None:
            return {"ok": False,
                    "error": "I couldn't read that file — it may not be a text document."}
        clipped = text[:TEXT_FILE_MAX_CHARS]
        store.cache_material(record["id"], {"text": clipped, "intake": "document"})
        return {"ok": True, "text": clipped, "intake": "document"}

    return {"ok": False, "error": "There's nothing to read."}


def _context_line(record: dict[str, Any]) -> str:
    identity = record.get("identity") or {}
    where = f" ({record['source']['url']})" if record.get("source", {}).get("url") else ""
    return f"This is {describe(identity)}{where}."


def _examine_media(record: dict[str, Any], request: str) -> dict[str, Any]:
    """Really watch, see or listen — walking the candidates, because preparation
    can fail for the top-ranked model specifically while the next is fine."""
    from ..gateway.client import ask
    from ..gateway.routing import Task, build_candidates

    source = record["source"]
    if source.get("kind") == "youtube":
        need = {"video": True}
    else:
        kind = file_kind(source["filePath"])
        is_pdf = mime_type_for(source["filePath"]) == "application/pdf"
        need = ({"video": True} if is_pdf or kind == "video"
                else {"audio": True} if kind == "audio" else {"vision": True})

    task = Task(text=request, needs_tools=False, background=True, need=need)
    candidates = build_candidates(task)
    if not candidates:
        return {"ok": False,
                "error": "None of your models can take that kind of file right now."}

    last_error: str | None = None
    for entry in candidates:
        try:
            prepared = prepare_for(entry, source)
        except CannotPrepare as err:
            last_error = str(err)
            continue
        except Exception as err:  # noqa: BLE001 — an upload failing is not fatal here
            last_error = str(err)
            continue

        try:
            answer = ask(f"{_context_line(record)}\n\nQuestion: \"{request}\"",
                         system=MEDIA_SYSTEM, want_json=True, task=task,
                         media=prepared["media"], model_id=entry["id"],
                         # The media is pinned to THIS model's key; falling back
                         # would hand the next provider a URI it cannot read.
                         only=True)
        except Exception as err:  # noqa: BLE001
            last_error = str(err)
            continue
        finally:
            if callable(prepared.get("cleanup")):
                prepared["cleanup"]()

        data = answer.data if isinstance(answer.data, dict) else None
        if data and data.get("answer"):
            intake = ("video" if source.get("kind") == "youtube"
                      else "document" if mime_type_for(source["filePath"]) == "application/pdf"
                      else file_kind(source["filePath"]))
            if data.get("observations"):
                store.cache_material(record["id"], {"observations": data["observations"],
                                                    "intake": intake})
            return {"ok": True, "answer": data["answer"], "intake": intake, "sources": []}
        last_error = last_error or "that model didn't answer in a usable form"

    if source.get("kind") == "youtube":
        return _youtube_fallback(record, request, last_error)
    return {"ok": False, "error": last_error or "None of your models could take that file."}


def _youtube_fallback(record: dict[str, Any], request: str,
                      last_error: str | None) -> dict[str, Any]:
    """Nothing could watch it. Read what is publishable instead, and be explicit
    about which of the two actually happened."""
    from ..gateway.client import ask
    from ..gateway.routing import Task

    fallback = fetch_youtube_text(record["source"]["url"])
    if not fallback.get("ok"):
        detail = f" ({last_error})" if last_error else ""
        return {"ok": False, "error": f"{fallback.get('error')}{detail}"}

    parts = []
    if fallback.get("title"):
        parts.append(f"Title: {fallback['title']}")
    if fallback.get("author"):
        parts.append(f"Channel: {fallback['author']}")
    if fallback.get("description"):
        parts.append(f"Description:\n{fallback['description']}")
    if fallback.get("transcript"):
        parts.append(f"Transcript of what is said:\n{fallback['transcript']}")
    if not fallback.get("transcript") and not fallback.get("description"):
        return {"ok": False,
                "error": "That video has no captions or description I can read, and I "
                         "couldn't watch it."}

    intake = "captions" if fallback.get("transcript") else "youtube-text"
    text = "\n\n".join(parts)
    store.cache_material(record["id"], {"text": text, "intake": intake})

    caveat = ("\n\nNote: this is a transcript, not the video — you have NOT seen what was on "
              "screen. Say so if it matters to the answer."
              if fallback.get("transcript") else
              "\n\nNote: this is only the title and description — you neither watched nor "
              "heard it. Be explicit in the answer about how little you actually have.")
    answered = ask(f"{_context_line(record)}\n\nContent:\n{text}\n\nQuestion: \"{request}\"",
                   system=TEXT_SYSTEM + caveat,
                   task=Task(text=request, needs_tools=False, background=True))
    return {"ok": True, "answer": answered.text, "intake": intake, "sources": [],
            "downgradedReason": last_error}


def _run_examine(record: dict[str, Any], request: str) -> dict[str, Any]:
    from ..gateway.client import ask
    from ..gateway.routing import Task

    if _is_text_shaped(record["source"]):
        text = _text_for(record)
        if not text["ok"]:
            return text
        answered = ask(
            f"{_context_line(record)}\n\nContent:\n{text['text']}\n\nQuestion: \"{request}\"",
            system=TEXT_SYSTEM, task=Task(text=request, needs_tools=False, background=True))
        return {"ok": True, "answer": answered.text, "intake": text["intake"], "sources": []}

    observations = (record.get("material") or {}).get("observations")
    if observations:
        quick = ask(f"{_context_line(record)}\n\nWorking notes from when I looked at this:\n"
                    f"{observations}\n\nNew question: \"{request}\"",
                    system=CACHED_FOLLOWUP_SYSTEM, want_json=True,
                    task=Task(text=request, needs_tools=False, background=True))
        data = quick.data if isinstance(quick.data, dict) else None
        if data and not data.get("needsAnotherLook") and data.get("answer"):
            return {"ok": True, "answer": data["answer"],
                    "intake": (record.get("material") or {}).get("intake") or "video",
                    "sources": []}
        # The notes did not cover it. Look again for real rather than guessing.
        fresh = _examine_media(record, request)
        if fresh.get("ok"):
            fresh["lookedAgain"] = True
        return fresh

    return _examine_media(record, request)


def _push_finding(record: dict[str, Any], finding: dict[str, Any], session_id: str) -> None:
    """Put the finding in the conversation itself, so "does that change my plan?"
    works later and the model can reason about what it already looked into."""
    body = "\n".join([
        f"(I looked into \"{(record.get('identity') or {}).get('title') or 'that'}\" — "
        f"they asked: \"{finding['request']}\"",
        f"How I took it in: {intake_description(finding.get('intake'))}",
        f"\n{finding['answer']}",
        f"\nReference id: {record['id']})",
    ])
    try:
        conversation.push_assistant_text(session_id, body)
    except Exception:  # noqa: BLE001
        logger.exception("could not add a finding for %s to the conversation", record["id"])


def _push_failure(record: dict[str, Any], request: str, error: str | None,
                  session_id: str) -> None:
    """The failure twin of `_push_finding`, and not a nicety.

    A failed examine that only announced itself over the event stream left no
    record the model could see — so "what did that turn up?" got nothing, on a
    roster where running out of quota mid-job is the normal case rather than an
    edge one.
    """
    body = "\n".join([
        f"(I tried to look into \"{(record.get('identity') or {}).get('title') or 'that'}\" — "
        f"they asked: \"{request}\"",
        f"\nThat didn't work: {error or 'something went wrong'}.",
        f"\nReference id: {record['id']})",
    ])
    try:
        conversation.push_assistant_text(session_id, body)
    except Exception:  # noqa: BLE001
        logger.exception("could not add a failure note for %s", record["id"])


def examine(content_id: str, request: str, *, session_id: str | None = None,
            event_bus: EventBus | None = None) -> dict[str, Any]:
    """Ask something about content already shared.

    Starts the work and returns immediately: the answer arrives in the
    conversation and over the event stream, because reading a long article or
    watching a video is not something to hold a reply open for.
    """
    record = store.get_content(content_id)
    if record is None:
        raise KeyError("I can't find that content any more.")
    text = (request or "").strip()
    if not text:
        raise ValueError("I didn't catch what you wanted to know.")

    target_session = session_id or record.get("sessionId") or "main"

    def work() -> None:
        _announce(record, event_bus=event_bus, status="working", instruction=text)
        current = store.get_content(content_id)
        if current is None:
            return                       # deleted mid-job; nothing to report
        try:
            outcome = _run_examine(current, text)
        except Exception as err:  # noqa: BLE001
            logger.exception("examining %s failed", content_id)
            outcome = {"ok": False, "error": f"Something went wrong looking into that: {err}"}

        if not outcome.get("ok"):
            _push_failure(current, text, outcome.get("error"), target_session)
            _announce(current, event_bus=event_bus, status="failed",
                      error=outcome.get("error"))
            return

        added = store.add_finding(content_id, request=text, answer=outcome["answer"],
                                  intake=outcome.get("intake"),
                                  sources=outcome.get("sources"))
        if added is None:
            return
        _push_finding(added["record"], added["finding"], target_session)
        _announce(added["record"], event_bus=event_bus, status="ready",
                  document=outcome["answer"], instruction=text,
                  lookedAgain=bool(outcome.get("lookedAgain")),
                  downgradedReason=outcome.get("downgradedReason"))

    run_in_background(work, name=f"examine:{content_id}")
    return record


# --- checking a claim ---------------------------------------------------------

VERDICTS = ("checks out", "partly true", "misleading", "false", "can't tell")

JUDGE_SYSTEM = """You judge whether a claim holds up, for someone deciding whether to act on it.

Rules:
- Base the verdict on the research you are given, not on your own impressions.
- "can't tell" is a real, acceptable verdict. Use it rather than guessing when the research does not settle the question.
- Say what is being left out, not only what is wrong. A claim can be technically true and still misleading about how typical the result is.
- Be specific about conditions: what has to be true for this to work, and how often that is the case.
- Never soften a verdict to be polite, and never sharpen one for effect."""


def judge_claim(claim: str, *, context: str | None = None,
                realism: bool = False) -> dict[str, Any]:
    """Research first, then judge. The research call is unconditional, at the
    top, with no path around it — that is the entire point of this function."""
    from ..gateway.client import ask
    from ..gateway.routing import Task

    question = (claim or "").strip()
    if not question:
        return {"ok": False, "error": "There was no claim to check."}

    found = research(
        (f'Is this realistic and typical, or is it exaggerated? "{question}". What actually '
         f"happens for most people who try this, and what does it require?") if realism else
        (f'Is this claim true? "{question}". Look for evidence for and against, and typical '
         f"real-world figures."),
        search_query=to_search_query(question))

    breakdown_ask = (
        'Fill in "breakdown" whenever the underlying thing being described is real and someone '
        "could actually go and do it — even if this particular claim about it is exaggerated. "
        "Give the honest step by step: steps in order, how long each really takes, what it "
        "costs, what is needed before starting, and where most people give up. Set it to null "
        "only if the whole thing is bogus and nobody should attempt it.")

    research_block = (
        f"Research findings:\n{found.answer}\n\nSources:\n" +
        "\n".join(f"[{i + 1}] {s.title} — {s.url}" for i, s in enumerate(found.sources))
        if found.ok else
        f"I could not research this ({found.error}). Say so in your reasoning and lean "
        f"towards \"can't tell\".")

    judged = ask(
        (f"{context}\n\n" if context else "") +
        f'The claim being judged: "{question}"\n\n{research_block}\n\n{breakdown_ask}\n\n'
        f'Reply as JSON:\n{{\n  "verdict": one of {list(VERDICTS)},\n'
        '  "confidence": "high" | "medium" | "low",\n'
        '  "reasoning": "a few sentences in plain language",\n'
        '  "whatsLeftOut": "what is being left out, or null",\n'
        '  "breakdown": "markdown, step by step" or null\n}',
        system=JUDGE_SYSTEM, want_json=True,
        task=Task(text=question, needs_tools=False, background=True))

    data = judged.data if isinstance(judged.data, dict) else {}
    verdict = str(data.get("verdict", "")).lower()
    if verdict not in VERDICTS:
        verdict = "can't tell"

    bits = [f"**Verdict: {verdict}**"
            + (f" ({data['confidence']} confidence)" if data.get("confidence") else "")]
    bits.append(f'\nOn this claim: *"{question}"*')
    if data.get("reasoning"):
        bits.append(f"\n{data['reasoning']}")
    if data.get("whatsLeftOut"):
        bits.append(f"\n**What it doesn't tell you:** {data['whatsLeftOut']}")
    if data.get("breakdown"):
        bits.append(f"\n## What's actually involved\n\n{data['breakdown']}")
    if not found.ok:
        bits.append(f"\n*I couldn't look this up independently — {found.error}*")

    return {"ok": True, "result": "\n".join(bits), "verdict": verdict,
            "sources": [{"title": s.title, "url": s.url} for s in found.sources]
            if found.ok else [],
            "researched": found.ok}


def describe_status(record: dict[str, Any] | None) -> str:
    """One spoken line. Voice must never read a whole finding out loud."""
    if not record:
        return "I can't find that."
    name = (record.get("identity") or {}).get("title") or "that"
    if not record.get("findings"):
        return f"I've got {name} — what would you like to know about it?"
    return f"{name} is ready."
