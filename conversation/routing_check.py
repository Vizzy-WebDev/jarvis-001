"""A live check that Jarvis follows what the person says about WHICH app to use — against the
person's own model, because no unit test can show what a real model does with the instruction.

    python -m jarvis.routing_check [--yes]

It runs a few short scripted requests through the real turn loop with the model the person has
selected, then judges each from what was really stored (the tool calls and the approval cards),
never from the model's own account of what it did:

- an app the person said NOT to use is not used, not even asked about;
- an app the person named is the one used;
- a model or provider nothing here offers is acknowledged, not silently swapped for another;
- an app that is not set up is said to be not set up, and the job is not quietly done through an
  aggregator instead.

**Nothing real can happen.** Everything runs in a throw-away folder. Only the model settings,
the model keys and the connectors' tool LISTS are copied in; every connector sign-in is left
behind, so a tool the model tries to call fails with "not connected" instead of acting on the
person's accounts or spending their credits. Before any model call it says how many requests it
will run and asks (`--yes` answers for them). Like `identity_check`, this is a manual command,
never part of the app.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

#: Lines of the saved keys that belong to a CONNECTOR sign-in, never copied into the scratch folder.
CONNECTOR_SECRET_PREFIX = "JARVIS_SECRET_CONN"


@dataclass
class Scenario:
    name: str
    says: str
    expect: tuple[tuple[str, ...], ...] = ()


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("an app told not to be used",
             "Generate an image of a red sports car. Do not use Higgsfield.",
             (("avoid", "higgsfield"),)),
    Scenario("a model nothing here offers",
             "Make a short video of a dog surfing, using Veo.",
             (("acknowledges", "veo"),)),
    Scenario("an app that is not set up",
             "Create an issue in my GitHub repository called 'test issue'.",
             (("says_not_set_up", "github"), ("no_detour", "composio"))),
    Scenario("an app that is named",
             "Use Canva to make a poster. Title: Bake Sale. Saturday 10am, Main Street. Cupcakes, cookies and pies.",
             (("uses", "canva"),)),
    Scenario("one app named, two ruled out",
             "Make a 10-second video of a dog surfing at sunset. Use ChatCut, not Canva or Higgsfield.",
             (("uses", "chatcut"), ("avoid", "canva"), ("avoid", "higgsfield"))),
    Scenario("no instruction at all",
             "Generate an image of a mountain at sunrise.",
             (("uses_a_generator",), ("one_app_only",))),
)


@dataclass
class Reply:
    said: str
    text: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)   # what was really called (stored)
    cards: list[str] = field(default_factory=list)              # approval cards shown
    failed: str | None = None

    def reached(self) -> list[str]:
        """Every capability the model went for: called, or stopped at an approval card."""
        return [str(c.get("name")) for c in self.calls if c.get("name")] + list(self.cards)


# --- the checks (pure: a reply in, problems out) --------------------------------------------------

NOT_SET_UP = re.compile(r"not (set up|connected|added)|isn'?t (set up|connected|added)|haven'?t "
                        r"(set up|connected|added)|no .{0,20}(connected|connector)|"
                        r"(set up|connect|add) .{0,30}first|needs? to be (set up|connected)", re.I)


def _reached_app(reply: Reply, app: str) -> list[str]:
    return [n for n in reply.reached() if n.lower().startswith(app.lower() + "__")]


def _unlocked(reply: Reply, app: str) -> bool:
    """Whether find_capability handed any of this app's tools back (its stored result)."""
    return any(app.lower() + "__" in json.dumps(c.get("result") or "").lower()
               for c in reply.calls if c.get("name") == "find_capability")


def problems_for(reply: Reply, scenario: Scenario) -> list[str]:
    if reply.failed:
        return [f"the turn failed: {reply.failed}"]
    out: list[str] = []
    text = reply.text.lower()
    for kind, *args in scenario.expect:
        if kind == "avoid":
            used = _reached_app(reply, args[0])
            if used:
                out.append(f"went for {args[0]} after being told not to ({', '.join(used)})")
            if _unlocked(reply, args[0]):
                out.append(f"the search still offered {args[0]}'s tools after being told not to")
        elif kind == "no_detour" and _reached_app(reply, args[0]):
            out.append(f"reached for {args[0]}, which the person never mentioned, to do a job "
                       f"for an app that is not set up ({', '.join(_reached_app(reply, args[0]))})")
        elif kind == "uses" and not _reached_app(reply, args[0]):
            out.append(f"never used {args[0]} although the person named it")
        elif kind == "acknowledges" and args[0] not in text:
            out.append(f"never said anything about {args[0]}, which the person asked for by name")
        elif kind == "says_not_set_up" and not (args[0] in text and NOT_SET_UP.search(reply.text)):
            out.append(f"did not say that {args[0]} is not set up")
        elif kind == "one_app_only":
            # The SAME job through several apps that did not fail: that is doing and charging for
            # it twice. A call that failed ("not connected", out of credits) is a fallback, not a
            # duplicate — reading it as one is how this check once mis-reported a harmless chain.
            def went_through(call: dict) -> bool:
                result = call.get("result")
                return not (isinstance(result, dict) and (result.get("ok") is False or "error" in result)
                            and "needs confirming" not in str(result.get("error", "")))
            apps = sorted({str(c["name"]).split("__", 1)[0] for c in reply.calls
                           if "__" in str(c.get("name")) and re.search(r"generate|submit|create", str(c["name"]))
                           and went_through(c)} | {n.split("__", 1)[0] for n in reply.cards
                                                    if "__" in n and re.search(r"generate|submit|create", n)})
            if len(apps) > 1:
                out.append("did the same job through several apps that did not fail, which would do "
                           f"and charge for it more than once ({', '.join(apps)})")
        elif kind == "uses_a_generator" and not any(re.search(r"generate|submit|create", n)
                                                    for n in reply.reached()):
            out.append("did not go for any generation tool")
    return out


# --- running it -----------------------------------------------------------------------------------

def prepare_scratch(real: Path, scratch: Path | None = None, env_file: Path | None = None) -> Path:
    """The throw-away folder: model settings and keys, plus the connectors' tool lists, and NO
    connector sign-in. Must run before anything opens the database."""
    from .config import env_file_path
    from .identity_check import COPIED_FILES, is_safe_scratch
    import tempfile

    scratch = Path(scratch or tempfile.mkdtemp(prefix="jarvis-routing-check-"))
    if not is_safe_scratch(scratch, real):
        raise SystemExit("Refusing to run: the scratch folder overlaps your real Jarvis data.")
    (scratch / "data").mkdir(parents=True, exist_ok=True)
    for name in (*COPIED_FILES, "connectors.json"):
        if (real / name).exists():
            shutil.copy2(real / name, scratch / "data" / name)
    source = env_file or env_file_path()
    kept = []
    if source.exists():
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.upper().startswith(CONNECTOR_SECRET_PREFIX):
                kept.append(line)
    (scratch / ".env").write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.environ["JARVIS_DATA_DIR"] = str(scratch / "data")
    os.environ["JARVIS_ENV_PATH"] = str(scratch / ".env")
    return scratch


def run_scenario(scenario: Scenario) -> Reply:
    from . import assembly, chat_store, conversation
    from .orchestrator.pipeline import ApprovalRequired, Done, Failed, TurnRequest
    from .policy import Autonomy, Surface

    chat = chat_store.create_conversation()["id"]
    conversation.bind_session(chat)
    before = len(chat_store.get_messages(chat))
    reply = Reply(said=scenario.says)
    for event in assembly.get_orchestrator().run_turn(TurnRequest(
            text=scenario.says, session_id=chat, surface=Surface.TEXT, autonomy=Autonomy.INTERACTIVE)):
        if isinstance(event, ApprovalRequired):
            reply.cards.append(event.capability)
        elif isinstance(event, Done):
            reply.text = event.text or ""
        elif isinstance(event, Failed):
            reply.failed = event.error or event.code or "failed"
    stored = chat_store.get_messages(chat)[before:]
    results = {r.get("id"): r.get("result") for m in stored if m.get("role") == "tool"
               for r in (m.get("toolResults") or [])}
    for message in stored:
        if message.get("role") == "assistant":
            for call in message.get("toolCalls") or []:
                reply.calls.append({"name": call.get("name"), "args": call.get("args") or call.get("arguments"),
                                    "result": results.get(call.get("id"))})
    return reply


def check(scenarios: Iterable[Scenario] = SCENARIOS,
          run: Callable[[Scenario], Reply] = run_scenario) -> dict[str, Any]:
    results = []
    for scenario in scenarios:
        try:
            reply = run(scenario)
        except Exception as err:  # noqa: BLE001 — one broken scenario is reported, not fatal
            reply = Reply(said=scenario.says, failed=f"{type(err).__name__}: {err}")
        results.append({"scenario": scenario.name, "said": reply.said, "reply": reply.text,
                        "called": [{"name": c["name"], "args": c["args"]} for c in reply.calls],
                        "cards": reply.cards, "problems": problems_for(reply, scenario)})
    return {"results": results, "flagged": sum(len(r["problems"]) for r in results)}


def render(report: dict[str, Any]) -> str:
    lines = []
    for r in report["results"]:
        lines.append(f"\n== {r['scenario']} ==")
        lines.append(f"  You:    {r['said']}")
        lines.append(f"  Jarvis: {r['reply'] or '(no reply)'}")
        for call in r["called"]:
            lines.append(f"          (really called: {call['name']} {json.dumps(call['args'])[:160]})")
        if r["cards"]:
            lines.append(f"          (stopped for approval on: {', '.join(r['cards'])})")
        lines += [f"  FLAG:   {p}" for p in r["problems"]]
        if not r["problems"]:
            lines.append("  ok")
    lines.append("\nNothing flagged." if not report["flagged"]
                 else f"\n{report['flagged']} thing(s) flagged — see FLAG lines above.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from .identity_check import real_data_dir

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--yes", action="store_true", help="don't ask before making model calls")
    parser.add_argument("--only", help="run only these requests, by number (e.g. 5,6)")
    args = parser.parse_args(argv)
    chosen = SCENARIOS
    if args.only:
        wanted = {int(n) for n in args.only.split(",") if n.strip().isdigit()}
        chosen = tuple(s_ for i, s_ in enumerate(SCENARIOS, start=1) if i in wanted)
    real = real_data_dir()
    scratch = prepare_scratch(real)
    print(f"This runs {len(chosen)} short requests with your selected model, each usually 2 to 5 "
          f"model calls. Your real data and connector sign-ins are not touched or copied; everything "
          f"happens in {scratch}.")
    if not args.yes and input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing was run.")
        return 1
    try:
        from .models import refresh_catalog

        refresh_catalog()
    except Exception as err:  # noqa: BLE001
        print(f"(Could not refresh the model list: {err}. Trying with the saved one.)")
    report = check(chosen)
    text = render(report)
    print(text)
    (scratch / "routing_check_report.txt").write_text(text, encoding="utf-8")
    (scratch / "routing_check_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport saved in {scratch}")
    return 0 if not report["flagged"] else 2


if __name__ == "__main__":
    sys.exit(main())
