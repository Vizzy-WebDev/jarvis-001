"""The morning briefing: facts gathered in code, narrated by the model.

**The guarantee this file exists to enforce: the model narrates, it never
supplies.** Every fact — the date, what is scheduled today, the weather, the
headlines, what the user has said matters to them — is gathered here, in code,
before any model call, and the prompt says plainly that anything not listed is
not known. A briefing that invents a meeting is worse than no briefing, because
it is indistinguishable from a real one.

That is also why the narration turn is TOOLLESS. Nothing it says can come from
anywhere except the facts handed to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..gateway.client import NoModelAvailable, ask
from ..gateway.routing import Task as RoutingTask
from ..memory import store as memory_store
from .briefing_config import get_config
from .recurrence import describe
from .task_store import list_tasks

logger = logging.getLogger(__name__)


@dataclass
class Briefing:
    ok: bool
    text: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    model_id: str | None = None
    error: str | None = None


def gather_facts(config: dict[str, Any] | None = None,
                 now: datetime | None = None) -> dict[str, Any]:
    """Everything the briefing is allowed to mention. No model call happens here."""
    config = config or get_config()
    sections = config.get("sections") or {}
    now = now or datetime.now()
    facts: dict[str, Any] = {}

    if sections.get("dateTime"):
        hour = now.hour % 12 or 12
        facts["now"] = {
            "date": f"{now:%A, %B} {now.day}, {now.year}",
            "time": f"{hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}",
            "partOfDay": ("morning" if now.hour < 12 else
                          "afternoon" if now.hour < 18 else "evening"),
        }

    if sections.get("tasks"):
        facts["tasks"] = [
            {"title": t["title"], "when": describe(t.get("recurrence")),
             "nextRunAt": t.get("nextRunAt")}
            for t in list_tasks() if t.get("enabled")
        ]

    if sections.get("goals"):
        # What the user has actually said matters, not an inference about it.
        facts["remembered"] = [m["text"] for m in memory_store.list_memories()[:8]]

    place = (config.get("weatherPlace") or "").strip()
    if place:
        from ..tools.get_weather import _run as weather

        result = weather(place=place)
        # Recorded either way: "I couldn't get the weather" is itself a fact the
        # briefing may state, and is far better than silence the user reads as
        # "there is nothing to say".
        facts["weather"] = result if result.get("ok") else {"ok": False,
                                                            "error": result.get("error")}

    if config.get("headlines"):
        from ..tools.get_headlines import _run as headlines

        result = headlines(count=4)
        facts["headlines"] = result.get("headlines") if result.get("ok") else []

    if sections.get("custom") and (config.get("customText") or "").strip():
        facts["custom"] = config["customText"].strip()

    return facts


def facts_to_prompt(facts: dict[str, Any], config: dict[str, Any],
                    using_connectors: bool = False) -> str:
    lines = [
        "Give the user their briefing, out loud, in your own voice.",
        "",
        "These are the ONLY facts you have. Anything not listed here, you do not "
        "know — say nothing about it, and never fill a gap with something "
        "plausible. If a section came back empty, either say so briefly or leave "
        "it out; do not invent an entry to fill the space.",
        "",
    ]
    if using_connectors:
        # The wording above would be false with tools on the table, and a stated
        # rule the model can see is not true is worse than no rule: it teaches
        # that the rules here are approximate.
        lines += [
            "They have also chosen to let this briefing use some of their connected "
            "apps. You have real tools for exactly those apps and no others. If "
            "checking one would genuinely add something, call it; otherwise skip it "
            "rather than forcing it in. The same rule holds either way: say only "
            "what a tool actually returned, never a guess about what it might say.",
            "",
        ]
    if facts.get("now"):
        lines.append(f"Now: {facts['now']['time']} on {facts['now']['date']} "
                     f"({facts['now']['partOfDay']}).")
    if "tasks" in facts:
        if facts["tasks"]:
            lines.append("Scheduled:")
            lines += [f"- {t['title']} ({t['when']})" for t in facts["tasks"]]
        else:
            lines.append("Scheduled: nothing.")
    if facts.get("weather"):
        weather = facts["weather"]
        lines.append(f"Weather: {weather['temperatureF']}F and {weather['condition']} "
                     f"in {weather['place']}." if weather.get("ok")
                     else f"Weather: unavailable ({weather.get('error')}).")
    if facts.get("headlines"):
        lines.append("Headlines:")
        lines += [f"- {h}" for h in facts["headlines"]]
    if facts.get("remembered"):
        lines.append("Things you remember about them (context, not items to read out):")
        lines += [f"- {m}" for m in facts["remembered"]]
    if facts.get("custom"):
        lines.append(f"They also asked you to include: {facts['custom']}")

    if not (config.get("sections") or {}).get("greeting"):
        lines += ["", "Skip any greeting; start straight with the content."]
    return "\n".join(lines)


#: What a briefing says out loud, whichever path composes it.
NARRATOR = ("You are giving the user their briefing out loud. You narrate only "
            "the facts you are given, in plain spoken sentences, warmly and "
            "briefly. You never invent an item, a time, or a detail.")


def connector_tool_names(config: dict[str, Any]) -> list[str]:
    """Real tool names for every connector the person picked for this briefing.

    Resolved HERE, at compose time, every time. A connector's tool list changes
    when it is reconnected, so a saved name would go stale silently; a saved id
    that no longer resolves simply contributes nothing.
    """
    from ..connectors.capabilities import tool_names_for

    names: list[str] = []
    for connector_id in config.get("connectors") or []:
        if connector_id:
            names.extend(tool_names_for(connector_id))
    return names


def compose_briefing(now: datetime | None = None) -> Briefing:
    """Gather the facts in code, then have a model narrate exactly those.

    **Everything above is gathered in code and only narrated**, which is the
    guarantee this file exists to keep: a briefing cannot invent an item because
    the model is never in a position to fetch one.

    **Chosen connectors are the one deliberate exception**, and it is explicit:
    the person picked exactly these, so the turn is allowed to actually call
    their tools on top of narrating what was gathered. With none picked — the
    default — this stays a narration-only turn with no tool access at all, which
    is the cheaper path as well as the stricter one.
    """
    config = get_config()
    facts = gather_facts(config, now)
    tools = connector_tool_names(config)
    prompt = facts_to_prompt(facts, config, using_connectors=bool(tools))

    if not tools:
        try:
            answer = ask(
                prompt, system=NARRATOR,
                # Nobody is waiting in real time, so cost matters more than latency.
                task=RoutingTask(text="briefing", background=True, needs_tools=False),
            )
        except NoModelAvailable as err:
            return Briefing(ok=False, facts=facts, error=str(err))
        return Briefing(ok=True, text=answer.text.strip(), facts=facts,
                        model_id=answer.model_id)

    return _compose_with_connectors(prompt, facts, tools)


def _compose_with_connectors(prompt: str, facts: dict[str, Any],
                             tools: list[str]) -> Briefing:
    """The tool-using path: a real turn, restricted to exactly what was chosen.

    Its own ephemeral session, never bound to chat history, so a briefing's
    working turns cannot appear in the conversation list. `addressed` is what
    separates this from a scheduled task's or a background job's turn, which
    also run in the background: those are not spoken to anyone, and this is.
    """
    import uuid

    from ..assembly import get_orchestrator
    from ..orchestrator import Done, Failed, TurnRequest
    from ..policy import Autonomy, Surface

    request = TurnRequest(
        text=prompt,
        session_id=f"briefing:{uuid.uuid4().hex[:8]}",
        surface=Surface.SCHEDULED,
        # Pre-consent covers ordinary work. A HIGH-risk action still parks for a
        # person — the owner's rule, enforced by the policy, not here.
        autonomy=Autonomy.PRE_CONSENTED,
        turn_id=uuid.uuid4().hex,
        allowed_names=frozenset(tools),
    )

    text, failure, model_id = "", None, None
    for event in get_orchestrator().run_turn(request):
        if isinstance(event, Done):
            text, model_id = event.text, event.model_id
        elif isinstance(event, Failed):
            failure = event
    if failure is not None:
        return Briefing(ok=False, text=text, facts=facts, error=failure.error)
    return Briefing(ok=True, text=text.strip(), facts=facts, model_id=model_id)
