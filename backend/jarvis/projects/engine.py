"""Rough idea in, plan and ready-to-paste build prompts out — never on its own.

Five functions, and **none of them chains into another**. Starting a project
makes no model call at all; it is a notepad opening, not a process starting.
Each later step happens only when explicitly asked for. That is the whole
difference from a build that researched an idea the moment it was mentioned and
wrote the plan the instant its question queue emptied.

The last two steps read every decision noted along the way, not the plan
document alone — which is what stops something settled in conversation from
quietly vanishing by the time the handoff prompt is written.

A leaf as far as the tool loader is concerned: it imports the gateway, research
and conversation, never the loader, the executor or the orchestrator.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import conversation
from ..background import run_in_background
from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..research import research, to_search_query
from . import store
from .assistants import describe_assistant, guidance_for

logger = logging.getLogger(__name__)

#: Enough conversation to pick up something mentioned in passing, short enough
#: not to crowd out the research and the decisions.
CONTEXT_MESSAGES = 14
CONTEXT_MAX_CHARS = 6000

PLAN_SYSTEM = """You write first-version project plans for someone who is not a programmer and will hand the work to an AI to build.

Rules:
- Plain language throughout. If a technical term is unavoidable, explain it in the same sentence.
- Be decisive. Recommend one approach and say why, rather than listing options.
- Ruthless about scope: the first version should be the smallest thing that is genuinely useful.
- Concrete numbers where you can — real costs, real timescales. Say when you are estimating.
- Never invent facts about services or prices. If you are unsure, say so.
- Treat every settled decision you are given as fixed — do not silently override or re-litigate one.
- Use markdown headings and bullets. This is a document to read, not something spoken aloud."""

PROMPTS_SYSTEM = ("You write prompts for AI assistants that will actually build something. "
                  "Output only the prompt text itself in each \"text\" field — no preamble, "
                  "no explanation, no surrounding quotes, no commentary about the prompt.")


def _conversation_context(session_id: str | None) -> str:
    """What has already been said, as plain text for a prompt. Read fresh every
    time, so this is the one seam a richer memory plugs into unchanged."""
    if not session_id:
        return ""
    try:
        messages = conversation.get_messages(session_id)
    except Exception:  # noqa: BLE001 — no context is a degraded plan, not a failed one
        return ""

    lines = [f"{'They said' if m.get('role') == 'user' else 'I said'}: {m['text']}"
             for m in messages[-CONTEXT_MESSAGES:]
             if m.get("role") in ("user", "assistant") and m.get("text")]
    if not lines:
        return ""
    joined = "\n".join(lines)
    clipped = f"…\n{joined[-CONTEXT_MAX_CHARS:]}" if len(joined) > CONTEXT_MAX_CHARS else joined
    return ("What we have been talking about (use anything relevant — do NOT make them "
            f"repeat it):\n\n{clipped}")


def _decisions_block(project: dict[str, Any]) -> str:
    decisions = project.get("decisions") or []
    if not decisions:
        return ""
    listed = "\n".join(f"- {d['text']}" for d in decisions)
    return ("Decisions already settled in conversation — treat these as fixed, do not re-ask "
            f"about them or contradict them:\n{listed}")


def _announce(project: dict[str, Any] | None, *, event_bus: EventBus | None = None,
              **extra: Any) -> None:
    if not project:
        return
    (event_bus or default_bus).publish(EventType.JOB_UPDATED, {
        "kind": "project", "id": project["id"], "title": project.get("title"),
        "status": extra.pop("status", "working"), "error": project.get("error"), **extra})


def _push_step(project: dict[str, Any] | None, *, step: str, ok: bool,
               text: str | None = None, error: str | None = None) -> None:
    """Put a finished (or failed) step into the conversation itself.

    Without this the document exists only in the project record and in a UI
    event — so "what did that say again?" has nothing to read, and the model
    cannot refer to something it supposedly just produced.
    """
    if not project:
        return
    label = {"research": "the research", "plan": "the plan",
             "prompts": "the build prompt(s)"}.get(step, "that")
    name = project.get("title") or project.get("idea")
    body = (f"({name} — {label} is ready:\n\n{text}\n\nProject id: {project['id']})" if ok else
            f"({name} — I tried to prepare {label}, but it didn't work: "
            f"{error or 'something went wrong'}.\n\nProject id: {project['id']})")
    try:
        conversation.push_assistant_text(project.get("sessionId") or "main", body)
    except Exception:  # noqa: BLE001
        logger.exception("could not add the %s update for %s", step, project["id"])


# --- 1. start -----------------------------------------------------------------

def start_project(*, idea: str, session_id: str, target: dict[str, Any] | None = None,
                  event_bus: EventBus | None = None) -> dict[str, Any]:
    """Create the project. No model call and no background work."""
    project = store.create_project(idea=idea, session_id=session_id)
    if target:
        project = store.update_project(project["id"], {"target": target}) or project
    _announce(project, event_bus=event_bus, status="started")
    return project


# --- 2. note a decision -------------------------------------------------------

def note_decision(project_id: str, text: str, *,
                  event_bus: EventBus | None = None) -> dict[str, Any]:
    project = store.add_decision(project_id, text)
    if project is None:
        raise KeyError("That project no longer exists.")
    _announce(project, event_bus=event_bus, status="noted", decision=text)
    return project


# --- 3. research --------------------------------------------------------------

def research_project(project_id: str, *, event_bus: EventBus | None = None) -> dict[str, Any]:
    """Look into what building this actually involves. Only ever when asked."""
    project = store.get_project(project_id)
    if project is None:
        raise KeyError("That project no longer exists.")

    def work() -> None:
        _announce(project, event_bus=event_bus, status="working", step="research")
        found = research(
            f"What is involved in building {project['idea']}? Cover the practical steps, "
            f"typical costs, and common mistakes.",
            search_query=f"{to_search_query(project['idea'])} cost how to build")

        if store.get_project(project_id) is None:
            return                       # deleted mid-job
        if found.ok:
            store.update_project(project_id, {"research": {
                "answer": found.answer,
                "sources": [{"title": s.title, "url": s.url} for s in found.sources],
                "via": found.via}})
            store.record_step(project_id, "research", found.model_id)
        else:
            store.update_project(project_id, {"research": {
                "answer": None, "sources": [], "via": None, "error": found.error}})

        done = store.get_project(project_id)
        _push_step(done, step="research", ok=found.ok, text=found.answer, error=found.error)
        _announce(done, event_bus=event_bus, status="ready" if found.ok else "failed",
                  step="research", document=found.answer if found.ok else None,
                  error=None if found.ok else found.error)

    run_in_background(work, name=f"project-research:{project_id}")
    return project


# --- 4. write the plan --------------------------------------------------------

def _plan_prompt(project: dict[str, Any]) -> str:
    context = _conversation_context(project.get("sessionId"))
    decisions = _decisions_block(project)
    background = ((project.get("research") or {}).get("answer") or "")
    return "\n\n".join(filter(None, [
        f'The idea:\n\n"{project["idea"]}"',
        context,
        decisions,
        f"Background research:\n\n{background}" if background else "",
        """Write the plan with exactly these sections, in this order:

## What you're building
Two or three sentences. What it is and who it's for.

## The first version
What it must do (the short list), what can wait, and what you are deliberately leaving out.

## How it works
Walk through it in plain language, from the user's point of view.

## What it'll be built with
Your recommendation and one line on why. Name specific tools.

## Steps, in order
Numbered. Each step small enough to finish in one sitting.

## What you'll need to provide
Accounts, keys, content, decisions — anything that has to come from them.

## What it'll cost
Realistic figures, and say clearly which are estimates.

## What could go wrong
The few things most likely to trip this up, and what to do about each.""",
    ]))


def write_plan(project_id: str, *, event_bus: EventBus | None = None) -> dict[str, Any]:
    project = store.get_project(project_id)
    if project is None:
        raise KeyError("That project no longer exists.")

    def work() -> None:
        from ..gateway.client import ask
        from ..gateway.routing import Task

        _announce(project, event_bus=event_bus, status="working", step="plan")
        try:
            written = ask(_plan_prompt(project), system=PLAN_SYSTEM,
                          task=Task(text=project["idea"], needs_tools=False, background=True))
        except Exception as err:  # noqa: BLE001 — no model available is the common case
            failed = store.update_project(project_id, {"error": f"I couldn't write the plan. {err}"})
            _push_step(failed, step="plan", ok=False, error=str(err))
            _announce(failed, event_bus=event_bus, status="failed", step="plan", error=str(err))
            return

        if store.get_project(project_id) is None:
            return
        store.record_step(project_id, "plan", written.model_id)
        done = store.update_project(project_id, {"plan": written.text, "error": None})
        _push_step(done, step="plan", ok=True, text=written.text)
        _announce(done, event_bus=event_bus, status="ready", step="plan",
                  document=written.text)

    run_in_background(work, name=f"project-plan:{project_id}")
    return project


# --- 5. write the build prompt(s) ---------------------------------------------

def _prompts_prompt(project: dict[str, Any], target: dict[str, Any] | None) -> str:
    context = _conversation_context(project.get("sessionId"))
    decisions = _decisions_block(project)
    background = ((project.get("research") or {}).get("answer") or "")
    return "\n\n".join(filter(None, [
        f"Here is a project plan:\n\n{project['plan']}",
        context,
        decisions,
        f"Background research:\n\n{background}" if background else "",
        guidance_for(target),
        """Decide whether this is small enough to hand over as ONE prompt, or whether it should be split into several sequential prompts (Prompt 1, Prompt 2, ...) because it is too large or too risky to build correctly in one step. Most small, well-scoped first versions need only one prompt — do not split needlessly.

Reply as JSON:
{
  "prompts": [
    { "title": "a short label, 3-6 words", "text": "the exact prompt text, ready to paste" }
  ],
  "order": "if there is more than one, one or two sentences on the recommended order and why each step depends on the last. null if there is only one."
}""",
    ]))


def write_prompts(project_id: str, target: dict[str, Any] | None = None, *,
                  event_bus: EventBus | None = None) -> dict[str, Any]:
    """The handoff prompt(s), written from the plan, every decision, the research
    AND the conversation — not the plan document alone."""
    project = store.get_project(project_id)
    if project is None:
        raise KeyError("That project no longer exists.")
    if not project.get("plan"):
        raise ValueError("That project doesn't have a written plan yet — write the plan first.")

    chosen = target or project.get("target")
    updated = store.update_project(project_id, {"target": chosen}) or project

    def work() -> None:
        from ..gateway.client import ask
        from ..gateway.routing import Task

        _announce(updated, event_bus=event_bus, status="working", step="prompts")
        try:
            written = ask(_prompts_prompt(updated, chosen), system=PROMPTS_SYSTEM,
                          want_json=True,
                          task=Task(text=updated["idea"], needs_tools=False, background=True))
        except Exception as err:  # noqa: BLE001
            failed = store.update_project(project_id,
                                          {"error": f"I couldn't write the build prompt. {err}"})
            _push_step(failed, step="prompts", ok=False, error=str(err))
            _announce(failed, event_bus=event_bus, status="failed", step="prompts",
                      error=str(err))
            return

        if store.get_project(project_id) is None:
            return

        raw = (written.data or {}).get("prompts") if isinstance(written.data, dict) else None
        prompts = [{"n": i + 1,
                    "title": str(p.get("title") or f"Prompt {i + 1}").strip(),
                    "text": str(p.get("text") or "").strip()}
                   for i, p in enumerate(raw or []) if isinstance(p, dict)]
        prompts = [p for p in prompts if p["text"]]

        if not prompts:
            reason = ("I couldn't produce a usable prompt from that — the plan is still ready "
                      "on its own.")
            failed = store.update_project(project_id, {"error": reason})
            _push_step(failed, step="prompts", ok=False, error=reason)
            _announce(failed, event_bus=event_bus, status="failed", step="prompts",
                      error=reason, document=updated.get("plan"))
            return

        order = (str((written.data or {}).get("order") or "").strip() or None
                 if len(prompts) > 1 else None)
        store.record_step(project_id, "prompts", written.model_id)
        done = store.update_project(project_id, {"prompts": prompts, "promptOrder": order,
                                                 "error": None})
        body = "\n\n---\n\n".join(f"Prompt {p['n']} — {p['title']}\n\n{p['text']}"
                                  for p in prompts)
        if order:
            body += f"\n\nRecommended order: {order}"
        _push_step(done, step="prompts", ok=True, text=body)
        _announce(done, event_bus=event_bus, status="ready", step="prompts",
                  document=done.get("plan"), prompts=prompts, promptOrder=order,
                  target=describe_assistant(chosen))

    run_in_background(work, name=f"project-prompts:{project_id}")
    return updated


def describe_status(project: dict[str, Any] | None) -> str:
    """One spoken line. Voice must never read a whole document out loud."""
    if not project:
        return "I can't find that project."
    name = project.get("title") or "your project"
    prompts = project.get("prompts") or []
    if prompts:
        many = "a prompt" if len(prompts) == 1 else f"{len(prompts)} prompts"
        return f"{name} is ready, with {many} for {describe_assistant(project.get('target'))}."
    if project.get("plan"):
        return f"{name} has a plan written. Say the word when you want the build prompt."
    if project.get("research"):
        return f"I've looked into {name}. Ready to write the plan whenever you are."
    return f"{name} is a work in progress — we're still talking it through."
