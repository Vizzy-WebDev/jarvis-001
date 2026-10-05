"""Who the finished build prompt is written *for*.

The same plan needs a completely different prompt depending on what is going to
build it. A terminal coding agent wants file paths, ordered steps and a way to
check its own work. A chat window has none of your files and needs everything
restated up front. An app builder wants to hear about screens and feel, not
folder structure. Handing all three the same text wastes most of the plan.

Plain data, so adding an assistant later is one entry here and nothing else.
`custom` covers anything not listed: the user names the tool, and its guidance
is written on the spot by whoever calls `guidance_for()`.

A leaf: no imports at all.
"""

from __future__ import annotations

from typing import Any

ASSISTANTS: list[dict[str, Any]] = [
    {
        "id": "claude-code",
        "label": "Claude Code",
        "blurb": "A coding agent working in a terminal on your actual files",
        "guidance": """This prompt goes to an autonomous coding agent that works directly in a real project folder, can read and write files, and can run commands.

Write it so that agent can start immediately:
- Open with one paragraph on what is being built and who for.
- State the tech stack decisively. Do not offer choices — an agent given options will pick badly or ask.
- Give the file and folder layout it should create, as a tree.
- Break the work into numbered steps small enough to finish and check one at a time. After each step, say how the agent should verify that step actually works (a command to run, a page to load, expected output).
- List the things it must NOT do — dependencies to avoid, files not to touch, scope not to expand into.
- End with what "done" looks like.""",
    },
    {
        "id": "chat",
        "label": "ChatGPT or Claude (chat window)",
        "blurb": "A chat assistant that cannot see your files",
        "guidance": """This prompt goes into a plain chat window. The assistant cannot see the user's files, cannot run anything, and remembers nothing beyond what this prompt contains.

Write it so it stands completely on its own:
- Restate every piece of context needed. Assume zero prior knowledge.
- Say what you want back and in what shape (full files, an explanation, a checklist) — be explicit, because there is no follow-up loop to correct it.
- Ask for complete, runnable code rather than snippets with gaps.
- Ask it to state assumptions rather than silently invent details.
- If the work is large, tell it to deliver in parts and wait to be asked for the next one.""",
    },
    {
        "id": "editor",
        "label": "Cursor, Windsurf or Copilot",
        "blurb": "An AI assistant inside a code editor",
        "guidance": """This prompt goes to an AI assistant embedded in a code editor. It can see the open project and edit files, but works best on one clear change at a time.

Write it as project direction plus a first task:
- Start with a short "project rules" block: stack, conventions, patterns to follow, things to never do.
- Then give the first concrete change, naming the files it should touch.
- Keep each instruction to a single file or a single feature — this kind of assistant degrades badly on sprawling multi-file requests.
- Say how the user will check the change worked before moving on.""",
    },
    {
        "id": "builder",
        "label": "Lovable, v0, Bolt or Replit",
        "blurb": "A tool you describe an app to and it builds it",
        "guidance": """This prompt goes to an app-building tool that turns a description into a working app. It responds to product and design language, not engineering instructions.

Write it the way you would brief a designer:
- What the app is, who uses it, and what they are trying to get done.
- Each screen, one by one: what is on it, what the user can do there, and where each action leads.
- The look and feel — mood, colours, whether it should feel playful or serious. Be concrete.
- What data it needs to hold, in plain words, not database terms.
- What the very first version must do, and explicitly what to leave out for now.
Avoid naming frameworks, file structures or libraries — this kind of tool chooses those itself and instructions about them confuse it.""",
    },
    {
        "id": "custom",
        "label": "Something else \u2014 I'll name it",
        "blurb": "Any other AI or coding assistant",
        "guidance": """The user named the tool that will receive this prompt. Write the prompt in whatever form suits that tool best, based on what it is.

If you genuinely do not know the tool, write a clear, self-contained brief: full context up front, an unambiguous statement of what is wanted, and explicit output expectations. That form works acceptably with any assistant.""",
    },
]


def list_assistants() -> list[dict[str, str]]:
    return [{"id": a["id"], "label": a["label"], "blurb": a["blurb"]} for a in ASSISTANTS]


def get_assistant(assistant_id: str | None) -> dict[str, Any] | None:
    return next((a for a in ASSISTANTS if a["id"] == assistant_id), None)


def guidance_for(target: dict[str, Any] | None) -> str:
    """The guidance block the prompt-writing step uses.

    A `custom` target carries the user's own name for the tool, passed through so
    the model can tailor to something this file has never heard of.
    """
    entry = get_assistant((target or {}).get("id")) or get_assistant("chat")
    if entry["id"] == "custom" and (target or {}).get("name"):
        return f"The tool that will receive this prompt is: {target['name']}.\n\n{entry['guidance']}"
    return entry["guidance"]


def describe_assistant(target: dict[str, Any] | None) -> str:
    """How the target is described back to the user ("a prompt for Cursor")."""
    if not target:
        return "an AI assistant"
    if target.get("id") == "custom":
        return target.get("name") or "the assistant you named"
    entry = get_assistant(target.get("id"))
    return entry["label"] if entry else "an AI assistant"
