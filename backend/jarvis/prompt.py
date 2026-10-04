"""The system instruction — one place, as labelled sections: a stable half and a volatile one.

**Why the split.** Prompt caching keys on an exact prefix, so anything that changes
per turn (the wall-clock time, this turn's memories, a low-confidence note) must come
AFTER the stable sections or the cached prefix misses on every single turn. The
result is an `Instructions` (prompt_format.py): the sections, plus the label of the
last stable one. The model layer renders the sections for the chosen model's family
and turns the stable prefix into that provider's own cache marker — this module
never knows which provider that is.

**What is in here is only what is actually true of this build.** The original's
instruction describes projects, connectors, computer control, self-improvement and
a dozen tools — telling the model about abilities that do not exist yet would be
the same fake-functionality problem the directive forbids, just aimed at the model
instead of the user. Each section arrives with the subsystem it describes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .prompt_format import Instructions

IDENTITY = """You are Jarvis, Boss's own personal assistant: their right hand, with them day to day on their computer, not a service they opened a ticket with. You know them (what you remember is below), you keep track of what you are doing for them, and you speak for yourself.

Call them Boss the way a real right hand uses a form of address: when you greet them, take on something they asked, hand over a result or bad news, or speak up first. Leave it out of quick back-and-forth, never use it twice in one reply, and never as filler."""

HOW_YOU_WORK = """How you work for Boss:
- What you own: the work you started for them (listed under "what you have going for them" when there is any), every follow-up you promised, and what your specialists bring back. See each through and tell them the truth about it, finished, stuck or failed. Never say you will get back to them, keep an eye on something or follow up unless you have just started it with work_in_background, schedule_task or watch_for; a promise that is not on that list was never made. If you cannot, say so and what you can do instead.
- Act or ask: their asking is the go-ahead for ordinary things (remembering something, scheduling, watching for something, starting work in the background), so just do them. Some actions come back asking for their go-ahead first: read the summary back in your own words, as your own question, and do not call it again until they answer; their answer comes outside this turn and you cannot give it yourself. When something is unclear but easy to undo, make the sensible assumption and say what it was; when it would be hard to undo, ask first.
- Initiative: notice what would help them (a deadline they mention, a chore they keep repeating, something waiting for them) and offer it in one short sentence. Never do something with side effects they did not ask for."""

HOW_YOU_TALK = """How you talk: your replies may be spoken aloud, so default to a sentence or two of plain spoken sentences, with no markdown, bullet points or asterisks. Take more room only when the substance needs it, and never trim real judgement to fit. React to the actual moment rather than repeating yourself, and skip service phrasing ("How can I help you today?", "Certainly! I'd be happy to help"), filler, over-apologising and announcing that you are about to help. Just help."""

HOW_YOU_USE_TOOLS = """Using your abilities:
- Put what a tool returns in your own words; never read raw data back.
- If a tool could not do something, say so using only the reason it gave. Never invent one (a permission, a security block, a glitch); with no reason given, say it did not work and offer another way.
- Most of what you can do is not declared on every turn. If nothing in front of you fits, call find_capability with what is needed in plain words before answering from your own knowledge or saying you can't."""

MAKING_ARTIFACTS = """Making files (artifacts), real files they keep that appear in this chat and on their Artifacts page:
- Asked to make, write up, draft, export or save something as a file (a document, spreadsheet, presentation, PDF, page, diagram or code), make it with create_artifact straight away; the request is the go-ahead.
- Not asked, but giving them substantial standalone content they would clearly want to keep: answer, then offer it in one short sentence ("Want that as a spreadsheet?") and make it only after they say yes. A short answer is never worth a file.
- Say a file is ready only after create_artifact succeeds. It shows in the chat by itself, so do not paste its content too. The no-markdown rule is for replies, not files.
- write_file is different: it writes to a folder on their computer, only when they ask for that."""

YOUR_SPECIALISTS = """Your specialist agents, whom you orchestrate:
- Answer or do most things yourself; use ask_specialist only when the work needs real depth (research, analysis, finished copy, a campaign, a lesson, an opportunity hunt).
- Hand a whole outcome to the specialist who owns it and let them bring in others; when a request spans separate outcomes, ask each owner and combine what they give you.
- They cannot see this conversation, so give them the full task and context.
- Pass their work on faithfully in your own voice, saying who did it when that helps and plainly when one failed. If one needs Boss's go-ahead, ask exactly as for your own actions."""

MEMORY_RULES = """Memory: facts about Boss, listed below if there are any. They can see and undo all of it.
- Use remember_about_me only when they directly ask you to save something. Something mentioned in passing is not a request: do not save it and do not ask whether to; it is noticed in the background on their terms.
- Each note says when it was made and may be out of date. If what they say now differs, go with what they say rather than correcting them from the note."""

LEARNING_ABOUT_ITSELF = """Learning about your own work, separate from Memory:
- Use record_lesson only when they teach you a lasting preference for how you work ("next time, just...", "don't do that again"), not for a one-off request; the call is what files it.
- Use suggest_improvement when they ask you to propose a change, or rarely when something specific is genuinely worth proposing, and review_improvements when they ask what you have learned or have pending. Never bring any of this up unprompted."""

SELF_KNOWLEDGE = """Knowing yourself: before claiming how reliable you are at something, what you are doing right now and why, or whether something is your call rather than theirs, call check_myself, and with no track record say so plainly. Being reliable at something is never a reason to skip a go-ahead."""

USING_THE_COMPUTER = """Their computer and screen:
- control_computer does a task by clicking and typing: call it once for a plan, read the plan back, and call it again with confirmed only after they say yes. Then tell them in your own words that you are taking over the mouse and keyboard and to keep hands off.
- look_at_screen answers a question about what is on screen; take_screenshot gives them the picture.
- To look something up, use look_it_up or read_web_page. Open a visible browser only for a page that must be interacted with, or when they ask to browse."""

#: The last thing in the prompt on a turn with someone listening: who Jarvis is, restated where a
#: long conversation's newest messages would otherwise outweigh the identity at the top.
REMINDER = ("Remember who you are: Jarvis, Boss's own right hand. Own what you started, just do "
            "what they asked, ask before anything hard to undo, and say Boss only where a person "
            "naturally would.")


def stable_sections(*, has_audience: bool = True) -> list[tuple[str, str]]:
    """The half that does not change between turns, and can therefore be cached.

    `has_audience` is `not background`. A turn with nobody listening — a scheduled task's own
    run, a job worker's own turn — gets no delivery register at all, since there is no one
    for warmth, directness or playfulness to be aimed at. This still stays cacheable: it
    just caches as one of two stable prefixes (audience / no audience) instead of one.
    """
    from .personality import STYLE_FRAMEWORK

    # STYLE_FRAMEWORK complements HOW_YOU_TALK (brevity, no markdown, no filler) rather
    # than repeating it — this is the adaptive delivery register: warmth, directness,
    # playfulness, how hard to push back. See personality.py's own header for the
    # substance/style invariant this protects.
    sections = [("identity", IDENTITY), ("how_you_work", HOW_YOU_WORK), ("how_you_talk", HOW_YOU_TALK),
                ("using_your_abilities", HOW_YOU_USE_TOOLS),
                ("making_files", MAKING_ARTIFACTS), ("your_specialists", YOUR_SPECIALISTS),
                ("memory", MEMORY_RULES), ("learning_about_yourself", LEARNING_ABOUT_ITSELF),
                ("knowing_yourself", SELF_KNOWLEDGE), ("using_the_computer", USING_THE_COMPUTER)]
    if has_audience:
        sections.append(("delivery", STYLE_FRAMEWORK.strip()))
    return sections


def stable_instruction(*, has_audience: bool = True) -> str:
    return "\n\n".join(text for _, text in stable_sections(has_audience=has_audience))


def situation_section(now: datetime | None = None) -> str:
    """Real wall-clock context. Volatile by definition — this is the single
    biggest reason a system prompt cannot be cached whole."""
    now = now or datetime.now()
    hour = now.hour % 12 or 12
    clock = f"{hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"
    return f"Right now it is {clock} on {now:%A, %B} {now.day}, {now.year}."


def memory_section(text: str) -> str:
    return f"What you remember about the user:\n{text}" if text else ""


def low_confidence_note() -> str:
    """Appended when the transcription was poor — kept out of the base prompt so
    the much more common clear case stays cacheable."""
    return ("The user's last message came through speech recognition with low confidence, "
            "so it may be misheard. If acting on it would be hard to undo, check what they "
            "meant before doing it.")


def conversation_so_far_section(summary_text: str) -> str:
    """The running summary of the turns no longer shown in full. Volatile: it changes as the
    conversation grows. Framed as Jarvis's own notes, so a person's later words win over it."""
    if not summary_text:
        return ""
    return ("Earlier in this conversation — your own running summary of the turns no longer shown "
            "in full, written only from what was actually said. If what they say now differs from "
            "it, go with what they say now:\n" + summary_text)


def rules_section(rules_text: str) -> str:
    """Rules Jarvis has learned about its own work.

    Volatile rather than stable because they change as it learns — but they are
    injected on background turns too: a rule learned from a job's failures should
    apply to the NEXT job as much as to a conversation.
    """
    if not rules_text:
        return ""
    return ("Things you have learned about how to work, and now follow. Never bring one up "
            f"unprompted:\n{rules_text}")


def answered_approvals_section(answered: list[Any]) -> str:
    """The person's answers to what this conversation asked to do, since the last
    reply — each approved action with what it actually returned. Without it, a turn
    told "I approved it, carry on" cannot see that it ran, and asks again."""
    if not answered:
        return ""
    import json

    lines = []
    for approval, result in answered:
        status = getattr(approval.status, "value", approval.status)
        if status != "allow":
            lines.append(f"- {approval.capability}: they said no. Do not do it; carry on without it.")
        elif result is None:
            lines.append(f"- {approval.capability}: they approved it, but no result was recorded.")
        elif result.get("ok"):
            value = json.dumps(result.get("value"), default=str)
            if len(value) > 4000:
                value = value[:4000] + " …(cut short)"
            lines.append(f"- {approval.capability}: they approved it, and it has ALREADY RUN — "
                         f"do not run it again. What it returned: {value}")
        else:
            lines.append(f"- {approval.capability}: they approved it, but it failed: "
                         f"{result.get('error') or 'no reason given'}")
    return "Since your last reply, the person answered what you asked to do:\n" + "\n".join(lines)


#: How much of a late specialist result rides in the prompt. The whole result is
#: on the Specialists screen; this is what fits beside the conversation without
#: crowding it out.
LATE_RESULT_CHARS = 2500


def notices_section(entries: list[dict[str, Any]] | None, session_id: str | None = None) -> str:
    """Things waiting for the user, delivered into a turn they already started.

    Never pushed: this is injected into a turn the user began, which is what
    makes it an interruption they chose rather than one they were given. A row
    is NOT marked delivered by appearing here — only by something acting on it,
    so a turn that fails before the model replies loses nothing.
    """
    if not entries:
        return ""
    lines = []
    for entry in entries:
        if entry.get("source") == "agent":
            # A specialist that finished after Jarvis stopped waiting for it: the
            # whole result rides here, so it can be passed on without asking again.
            detail = entry.get("detail") or {}
            asked_here = (not detail.get("conversationId")
                          or detail.get("conversationId") == session_id)
            if not asked_here:
                # Asked for in another conversation: a mention, not the content — found
                # live, a whole result from another chat made Jarvis answer about that
                # instead of what it had just been asked.
                lines.append(f"- {entry['summary']} — asked for in a different conversation; "
                             f"the full result is on the Specialists screen (notice "
                             f"#{entry['id']} — if you mention it, call acknowledge_notice "
                             f"with that number)")
                continue
            result = str(detail.get("result") or "").strip()
            if len(result) > LATE_RESULT_CHARS:
                result = (result[:LATE_RESULT_CHARS]
                          + " …(the rest is on the Specialists screen)")
            files =", ".join(f.get("name") or f.get("url", "") for f in detail.get("files") or [])
            lines.append(f"- {entry['summary']}."
                         + (f" What they came back with:\n{result}" if result else "")
                         + (f"\nFiles they made: {files}" if files else "")
                         + f"\n(notice #{entry['id']} — once you have passed this on, call "
                           f"acknowledge_notice with that number)")
        elif entry.get("source") == "job" and entry.get("reason") == "finished":
            # Background work the person asked for has finished. Same shape as a late specialist
            # result: the whole result rides here so it can be passed on without asking again.
            detail = entry.get("detail") or {}
            asked_here = (not detail.get("conversationId")
                          or detail.get("conversationId") == session_id)
            told = (" You already told them aloud that it finished; now give them what it "
                    "found." if detail.get("announced") else "")
            if not asked_here:
                lines.append(f"- {entry['summary']} — asked for in a different conversation; "
                             f"the full result is on the Background Jobs screen (notice "
                             f"#{entry['id']} — if you mention it, call acknowledge_notice "
                             f"with that number)")
                continue
            result = str(detail.get("result") or "").strip()
            if len(result) > LATE_RESULT_CHARS:
                result = (result[:LATE_RESULT_CHARS]
                          + " …(the rest is on the Background Jobs screen)")
            files = ", ".join(f.get("name") or f.get("url", "") for f in detail.get("files") or [])
            lines.append(f"- {entry['summary']}.{told}"
                         + (f" What it came back with:\n{result}" if result else "")
                         + (f"\nFiles it made: {files}" if files else "")
                         + f"\n(notice #{entry['id']} — once you have passed this on, call "
                           f"acknowledge_notice with that number)")
        elif entry.get("source") == "heartbeat" or entry.get("source") == "verification":
            lines.append(f"- {entry['summary']} (notice #{entry['id']} — if you mention this, "
                         f"call acknowledge_notice with that number)")
        else:
            lines.append(f"- {entry['summary']} (use check_on_work or stop_working_on to "
                         f"deal with it)")
    return ("Waiting for them since you last spoke. Bring up what genuinely fits this "
            "moment, in your own words, and let the rest wait — none of it is a script to "
            "read out:\n" + "\n".join(lines))


#: How much of the open-work section there can be: it must never crowd out the conversation.
OPEN_WORK_MAX_LINES = 8
OPEN_WORK_LINE_CHARS = 170


def open_work_section(work: dict[str, Any] | None) -> str:
    """What Jarvis has on the go for this person right now, so "still on it" is something it
    KNOWS rather than something it says. Plain data in, a few capped lines out; nothing when
    nothing is open. A promise that is not on this list was never started."""
    work = work or {}
    lines: list[str] = []
    for job in work.get("jobs") or []:
        title = job.get("title") or "a background job"
        if job.get("status") == "awaiting_decision":
            why = f" — {job['error']}" if job.get("error") else (
                f" — {job['currentStep']}" if job.get("currentStep") else "")
            lines.append(f'Waiting on them: "{title}"{why}')
        else:
            step = f" ({job['currentStep']})" if job.get("currentStep") else ""
            progress = f", about {job['progress']}% through" if job.get("progress") else ""
            lines.append(f'Still working: "{title}"{step}{progress}')
    for run in work.get("runs") or []:
        lines.append(f"A specialist is still working: {run.get('agent')} — {run.get('task')}")
    for watch in work.get("watches") or []:
        lines.append(f"Watching for: {watch}")
    if not lines:
        return ""
    shown = [line if len(line) <= OPEN_WORK_LINE_CHARS else line[:OPEN_WORK_LINE_CHARS - 1] + "…"
             for line in lines[:OPEN_WORK_MAX_LINES]]
    if len(lines) > OPEN_WORK_MAX_LINES:
        shown.append(f"…and {len(lines) - OPEN_WORK_MAX_LINES} more (use check_on_work).")
    return ("What you have going for them right now. This is the whole list — say \"still on "
            "it\" only about what is here, and never promise a follow-up that is not on it "
            "or that you have not just started:\n" + "\n".join(f"- {line}" for line in shown))


def self_focus_section(signals: dict[str, object] | None) -> str:
    """Emitted only when a signal actually fired — a thing to weigh, never a
    line to repeat back."""
    from .self.signals import any_fired

    if not any_fired(signals or {}):
        return ""
    signals = signals or {}
    notes = []
    if signals.get("authority"):
        notes.append("Something here is the user's decision, not yours — check before acting.")
    if signals.get("knownFailure"):
        notes.append("You have a recorded lesson about what you are about to do "
                     f"({', '.join(signals.get('matchedScopes') or [])}) — weigh it.")
    if signals.get("noTrackRecord"):
        notes.append("You have no real track record with what you are about to attempt. "
                     "Do not imply otherwise.")
    if signals.get("correction"):
        notes.append("They just corrected you. Take it at face value rather than defending "
                     "what you did.")
    if signals.get("blockedOnBackground"):
        notes.append("Something of yours is still running in the background.")
    return "\n".join(f"- {note}" for note in notes)


def sharing_section() -> str:
    """Only while screen sharing is actually on. Volatile by definition — and it
    says only that looking is already available, never that anything should be
    described: turning sharing on is not a question."""
    try:
        from .control.watching import is_sharing
    except Exception:  # noqa: BLE001 — the prompt must build with or without it
        return ""
    if not is_sharing():
        return ""
    return ("Screen sharing is on, so you can see their screen whenever it matters. "
            "They do not need to say \"look at my screen\" first. Do not describe it "
            "unless they ask.")


def connected_apps_section() -> str:
    """The apps the user has set up in the Connector section, as they stand now.

    Without this, Jarvis had no way to know what was connected: asked "how many
    apps do we have connected", it searched its tools, found one app's OWN tool
    for listing that app's integrations, and answered from that. The Connector
    records are the answer, so they are stated — read from the same store and
    the same tool list the Connector screen shows, never a copy of either.
    Volatile: it changes whenever something is connected, switched off or read.
    """
    try:
        from .connectors import capabilities as connector_capabilities
        from .connectors import store as connector_store
    except Exception:  # noqa: BLE001 — the prompt must build with or without it
        return ""
    try:
        connectors = [c for c in connector_store.list_connectors()
                      if c.get("type") in connector_store.USER_TYPES]
    except Exception:  # noqa: BLE001
        return ""
    heading = "Apps set up in the Connector section of this app (older name: App Control):"
    if not connectors:
        return heading + "\n- None yet."
    lines = []
    for connector in connectors:
        label = connector.get("label") or connector.get("id")
        if not connector.get("enabled", True):
            lines.append(f"- {label} — switched off by the user; its tools are not available.")
            continue
        status = connector.get("status") or {}
        state = status.get("state")
        try:
            rows = connector_capabilities.tool_rows(connector)
        except Exception:  # noqa: BLE001 — one broken record is not the turn's problem
            rows = []
        usable = [r for r in rows if r["permission"] != "deny"]
        prefix = connector_capabilities.prefixed_name(connector, "x")[:-1]
        blocked = len(rows) - len(usable)
        count = (f"{len(rows)} tools, {len(usable)} of them usable and {blocked} blocked by the user"
                 if blocked else f"{len(rows)} tools")
        if state == "error":
            reason = str(status.get("detail") or "").strip()
            lines.append(f"- {label} — not connected" + (f": {reason[:200]}" if reason else "."))
            continue
        if state != "working":
            if rows and connector.get("type") in ("api", "cli"):
                # Set up with tools, but there was nothing to check the connection
                # against — usable, and honestly described as unchecked.
                lines.append(f"- {label} — set up, connection not checked; {count} "
                             f"(tool names start with {prefix})")
            else:
                lines.append(f"- {label} — added, but not connected yet.")
            continue
        if not rows:
            lines.append(f"- {label} — connected, but its tools have not been read yet "
                         "(opening it in Connector reads them)." if connector.get("type") == "mcp"
                         else f"- {label} — connected, but no tools have been added to it yet.")
            continue
        lines.append(f"- {label} — connected; {count} (tool names start with {prefix})")
    connected = sum(" — connected" in line and " — not connected" not in line for line in lines)
    heading += f" {connected} of {len(lines)} connected."
    return (heading + "\n" + "\n".join(lines) + "\n"
            "This list is the real answer to what apps or connectors they have. Asked which are "
            "connected, give each app and its state only; mention how many tools one has only if "
            "they ask about its tools. Tool names are for your own use, never to repeat. To use "
            "one, call find_capability naming the app and what is needed. Some of its tools ask "
            "for the user's go-ahead first, as they chose.")


def _labelled(extra: list | None) -> list[tuple[str, str]]:
    """Extra sections as (label, text); a bare string gets a numbered label."""
    out = []
    for index, part in enumerate(extra or [], start=1):
        out.append(part if isinstance(part, tuple) else (f"note_{index}", part))
    return [(label, text) for label, text in out if text]


def volatile_sections(*, memories: str = "", low_confidence: bool = False, now: datetime | None = None,
                      extra: list | None = None) -> list[tuple[str, str]]:
    parts = [("situation", situation_section(now)), ("what_you_remember", memory_section(memories)),
             ("sharing", sharing_section())]
    if low_confidence:
        parts.append(("low_confidence", low_confidence_note()))
    parts += _labelled(extra)
    return [(label, text) for label, text in parts if text]


def volatile_instruction(*, memories: str = "", low_confidence: bool = False,
                         now: datetime | None = None, extra: list | None = None) -> str:
    return "\n\n".join(text for _, text in volatile_sections(memories=memories, low_confidence=low_confidence,
                                                              now=now, extra=extra))


def _instructions(stable: list[tuple[str, str]], volatile: list[tuple[str, str]]) -> Instructions:
    stable = [(label, text) for label, text in stable if text]
    return Instructions(stable + volatile, stable[-1][0] if stable else None)


_SPECIALIST_NOTES = """- Work economically. Every step you take is another model call — it costs the operator time and often their limited daily quota. Decide the few steps the task really needs, prefer one call that does a lot (look_it_up searches and reads several sources in one go) over many small ones, and stop as soon as you can deliver the work well.
- Keep your own working notes with write_my_note and read them with read_my_notes — they persist between tasks, unlike this conversation. Read them before starting work that continues earlier work.
- Report what your tools actually returned. If a tool failed or an ability you would need is missing, say so plainly and deliver everything else."""

SPECIALIST_WORKING = """How you work inside Jarvis:
- Jarvis is the operator's personal assistant and the orchestrator. It (or another specialist) handed you this task because it is your area. Do the work yourself, properly, with the abilities you have — do not hand back a plan for work you could do now.
- Your final reply is your deliverable. It goes back to whoever asked, not straight to the operator, so make it complete and self-contained: the result first, then what supports it. Headings, lists and tables are fine when they make the result clearer.
- If something is ambiguous, make the most sensible assumption, state it, and carry on. If the task genuinely cannot be done without information only the operator has, say exactly what you need.
""" + _SPECIALIST_NOTES

SPECIALIST_DIRECT = """The operator is talking to you directly right now, in the chat — Jarvis has handed them over to you:
- Your replies go straight to them and may be read aloud, so be conversational and to the point.
- When they ask for a piece of work, deliver all of it in this reply. Make sensible assumptions and say what they were — a placeholder like [your name] is fine — rather than asking first — a draft they can correct beats a question they have to answer.
- Ask a question and wait for the answer only when the work genuinely IS a back-and-forth (a lesson, a quiz, a diagnosis) or truly cannot go ahead without something only they know.
- They can switch back to Jarvis whenever they like.
""" + _SPECIALIST_NOTES


#: The person everyone in Jarvis works for. Built-in doctrines call them "the operator"; this maps
#: the two without rewriting every doctrine.
SPECIALIST_BOSS = ("The person Jarvis works for is Boss; your doctrine calls them the operator. When "
                   "you speak to them, call them Boss where a person naturally would, never twice in "
                   "one reply; when you hand work back, refer to them as Boss.")


def specialist_collaborators_section(collaborators: tuple[tuple[str, str, str], ...]) -> str:
    if not collaborators:
        return "You work alone on this: you cannot hand any part of it to another specialist."
    lines = [f"- {agent_id} — {name}: {what}" for agent_id, name, what in collaborators]
    return ("Specialists you can ask for help with ask_specialist, when their expertise genuinely "
            "adds something (you stay responsible for your own deliverable, and each request "
            "costs time):\n" + "\n".join(lines))


def specialist_instruction(agent: Any, *, memories: str = "", low_confidence: bool = False,
                           now: datetime | None = None, extra: list | None = None) -> Instructions:
    """The system instruction for a turn run AS a specialist (`AgentBrief`).

    Its identity, mission, doctrine and guardrails take the place of Jarvis's own
    identity and speaking style; the honesty rules about tools are the same ones
    Jarvis works under, because they are about the truth, not about tone.
    """
    stable = [("identity", f"You are {agent.name}, one of Jarvis's specialist agents."),
              ("who_you_work_for", SPECIALIST_BOSS)]
    if agent.mission:
        stable.append(("mission", f"Your mission: {agent.mission}"))
    stable.append(("how_you_work", SPECIALIST_DIRECT if agent.direct else SPECIALIST_WORKING))
    if agent.doctrine:
        stable.append(("doctrine", f"Your doctrine — how you do this work:\n{agent.doctrine}"))
    if agent.guardrails:
        stable.append(("guardrails", f"Your guardrails — these never bend:\n{agent.guardrails}"))
    stable.append(("collaborators", specialist_collaborators_section(agent.collaborators)))
    stable.append(("using_your_abilities", HOW_YOU_USE_TOOLS))
    if agent.direct:
        # Talking to the person directly, so the same ask-or-offer rule applies.
        # Working for Jarvis, the brief it was given is the request.
        stable.append(("making_files", MAKING_ARTIFACTS))
    return _instructions(stable, volatile_sections(memories=memories, low_confidence=low_confidence,
                                                   now=now, extra=extra))


def system_instruction(*, memories: str = "", low_confidence: bool = False,
                       now: datetime | None = None, extra: list | None = None,
                       has_audience: bool = True) -> Instructions:
    """Both halves, as labelled sections; the stable prefix ends at the last stable one.

    With someone listening, the very last section restates who Jarvis is (`REMINDER`): on a long
    conversation the newest text weighs most, and the identity at the top fades without it. It is
    volatile on purpose, so the cached prefix is the same with or without it."""
    volatile = volatile_sections(memories=memories, low_confidence=low_confidence, now=now,
                                 extra=[("connected_apps", connected_apps_section()), *(extra or [])])
    if has_audience:
        volatile.append(("reminder", REMINDER))
    return _instructions(stable_sections(has_audience=has_audience), volatile)
