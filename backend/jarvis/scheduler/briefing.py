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


def facts_to_prompt(facts: dict[str, Any], config: dict[str, Any]) -> str:
    lines = [
        "Give the user their briefing, out loud, in your own voice.",
        "",
        "These are the ONLY facts you have. Anything not listed here, you do not "
        "know — say nothing about it, and never fill a gap with something "
        "plausible. If a section came back empty, either say so briefly or leave "
        "it out; do not invent an entry to fill the space.",
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


def compose_briefing(now: datetime | None = None) -> Briefing:
    config = get_config()
    facts = gather_facts(config, now)
    try:
        answer = ask(
            facts_to_prompt(facts, config),
            system=("You are giving the user their briefing out loud. You narrate only "
                    "the facts you are given, in plain spoken sentences, warmly and "
                    "briefly. You never invent an item, a time, or a detail."),
            # Nobody is waiting in real time, so cost matters more than latency.
            task=RoutingTask(text="briefing", background=True, needs_tools=False),
        )
    except NoModelAvailable as err:
        return Briefing(ok=False, facts=facts, error=str(err))
    return Briefing(ok=True, text=answer.text.strip(), facts=facts, model_id=answer.model_id)
