"""Who Jarvis is (Phase 5): the prompt's identity, "Boss", what Jarvis owns, act vs ask, and the
live check (`jarvis/identity_check.py`).

What a prompt does to a real model cannot be proven here — that is what the live check is for, run
on the person's own machine. These tests pin everything that CAN be pinned: the order of the
prompt, its size, that no rule the old prompt carried was dropped, that the prompt never names an
ability that does not exist or contradicts the policy that really decides, and that the live check
flags exactly what it claims to and never touches real data.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from jarvis import assembly, conversation, identity_check, prompt
from jarvis.capabilities import Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.identity_check import Reply, Scenario, conversation_problems, reply_problems
from jarvis.orchestrator.context import AgentBrief, RelevanceContext, estimate_tokens
from jarvis.personality import STYLE_FRAMEWORK


@pytest.fixture
def app(scratch):
    reset_db()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    yield
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def stable() -> str:
    return prompt.stable_instruction()


def labels(instructions) -> list[str]:
    return [label for label, _ in instructions.sections]


# --- the order: who Jarvis is comes first, and is said again last -------------------------------------

def test_identity_comes_first_and_how_jarvis_works_for_boss_second():
    assert labels(prompt.system_instruction())[:2] == ["identity", "how_you_work"]
    assert prompt.system_instruction().startswith("You are Jarvis, Boss's own personal assistant")


def test_the_reminder_is_the_very_last_section_when_someone_is_listening():
    system = prompt.system_instruction(extra=[("waiting_notices", "something waiting"),
                                              ("style_now", "measured, this turn")])
    assert labels(system)[-1] == "reminder"
    assert system.sections[-1][1] == prompt.REMINDER
    # Volatile, so the cached prefix is the same with it as without it.
    assert system.stable_prefix_until == "delivery"
    assert labels(system).index("reminder") > labels(system).index("delivery")


def test_a_turn_with_nobody_listening_gets_no_reminder_and_no_delivery_register():
    system = prompt.system_instruction(has_audience=False)
    assert "reminder" not in labels(system) and "delivery" not in labels(system)
    assert prompt.REMINDER not in system
    # Still Jarvis: the identity is not a style, so background work keeps it.
    assert labels(system)[0] == "identity"


def test_the_real_assembly_puts_the_reminder_after_the_per_turn_style_floor(app):
    context = RelevanceContext().assemble(session_id="s1",
                                          text="I'm so overwhelmed, I can't keep up with this")
    names = labels(context.system)
    assert "style_now" in names, "the distress floor should have fired"
    assert names[-1] == "reminder" and names.index("style_now") < names.index("reminder")

    background = RelevanceContext().assemble(session_id="s1", text="anything", background=True)
    assert "reminder" not in labels(background.system)


def test_specialists_keep_their_own_identity_and_call_the_person_boss():
    for direct in (True, False):
        brief = AgentBrief(agent_id="teacher", name="Teacher", mission="Teach well.", direct=direct)
        system = prompt.specialist_instruction(brief)
        assert system.startswith("You are Teacher, one of Jarvis's specialist agents.")
        assert prompt.SPECIALIST_BOSS in system and "the operator" in prompt.SPECIALIST_BOSS
        assert "Jarvis, Boss's own personal assistant" not in system
        assert "reminder" not in labels(system)


# --- the size: identity no longer buried, and it cannot silently regrow ---------------------------------

def test_the_fixed_prompt_stays_within_its_size():
    # Measured with the same estimate the context budget uses (characters / 4). Before Phase 5 this
    # was ~3,930; the target was ~2,000. A failure here means the prompt grew back: shorten it,
    # don't raise the number.
    assert estimate_tokens(stable()) <= 2000
    assert estimate_tokens(STYLE_FRAMEWORK) <= 450


# --- "Boss" --------------------------------------------------------------------------------------------

def test_boss_is_used_like_a_real_form_of_address_and_the_old_contradiction_is_gone():
    identity = prompt.IDENTITY
    assert "Call them Boss" in identity
    assert "never use it twice in one reply" in identity
    assert "Leave it out of quick back-and-forth" in identity
    assert "never as filler" in identity
    # The old rule demanded it never "disappear" on plain replies — which pushes toward using it
    # mechanically, the opposite of what was asked.
    assert "disappear" not in stable()
    assert '"boss"' not in stable()


# --- nothing the old prompt promised was dropped -----------------------------------------------------------

#: Every rule the old prompt carried, by the words that carry it. Wording may change; a rule may not
#: silently go.
KEPT_RULES = {
    "make a file when asked": ["create_artifact straight away"],
    "offer a file when not asked": ["offer it in one short sentence", "only after they say yes"],
    "file ready only after it succeeds": ["only after create_artifact succeeds"],
    "write_file is different": ["write_file"],
    "find_capability before giving up": ["find_capability"],
    "never invent a failure reason": ["Never invent one"],
    "no promise without a real follow-up": ["Never say you will get back to them",
                                            "work_in_background", "schedule_task", "watch_for"],
    "an approval is answered outside the turn": ["do not call it again until they answer",
                                                  "cannot give it yourself"],
    "remember only when asked": ["remember_about_me only when they directly ask"],
    "passing mentions are not requests": ["mentioned in passing"],
    "old notes may be out of date": ["go with what they say"],
    "lessons only when taught": ["record_lesson only when they teach you"],
    "improvements": ["suggest_improvement", "review_improvements",
                     "Never bring any of this up unprompted"],
    "check_myself before self-claims": ["check_myself"],
    "control_computer confirms": ["control_computer", "with confirmed only after they say yes",
                                  "keep hands off"],
    "screen question vs screenshot": ["look_at_screen", "take_screenshot"],
    "invisible lookups first": ["look_it_up", "read_web_page"],
    "specialists": ["ask_specialist", "cannot see this conversation"],
    "no markdown": ["no markdown"],
    "no service phrasing": ["How can I help you today?"],
    "style never changes substance": ["never whether it is said or what it is"],
    "no manufactured pushback": ["Manufactured pushback"],
    "own assessment": ["instead of defaulting to agreement or praise"],
    "the laugh token": ["[[laugh]]", "at most once in a reply", "never at the very start"],
    "devil's advocate announced": ["devil's advocate", "before you start"],
    "criticise the work, not the person": ["never the person"],
    "crisis lines": ["your local crisis line", "emergency services"],
    "initiative is offered, not taken": ["offer it in one short sentence",
                                         "Never do something with side effects they did not ask for"],
    "easy to undo → assume; hard to undo → ask": ["easy to undo", "hard to undo, ask first"],
}


@pytest.mark.parametrize("rule", sorted(KEPT_RULES))
def test_no_rule_the_old_prompt_carried_was_dropped(rule):
    text = stable()
    for words in KEPT_RULES[rule]:
        assert words in text, f"{rule}: {words!r} is gone from the prompt"


# --- the prompt never contradicts what is really there ------------------------------------------------------

TOOL_NAME = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")


def test_every_ability_the_prompt_names_really_exists(app):
    registry = assembly.get_registry()
    for text in (stable(), prompt.REMINDER, prompt.SPECIALIST_BOSS, prompt.SPECIALIST_WORKING,
                 prompt.SPECIALIST_DIRECT):
        for name in set(TOOL_NAME.findall(text)):
            assert registry.get(name) is not None, f"the prompt names {name!r}, which does not exist"


#: What the "act or ask" line tells Jarvis to just do, and the tool each phrase means. Reviewed by
#: hand like Phase 3's REQUEST_SUFFICES: every one must really run without a card on a clear request.
JUST_DO = {"remembering something": "remember_about_me", "scheduling": "schedule_task",
           "watching for something": "watch_for", "starting work in the background": "work_in_background"}


def test_what_the_prompt_says_to_just_do_really_runs_without_asking(app):
    registry = assembly.get_registry()
    act_line = next(line for line in prompt.HOW_YOU_WORK.splitlines() if "Act or ask" in line)
    for phrase, name in JUST_DO.items():
        assert phrase in act_line, phrase
        spec = registry.get(name)
        assert spec.risk is Risk.LOW or (spec.risk is Risk.MEDIUM and spec.request_suffices), name
    # And the follow-up tools the promise rule names are the same "just do it" kind.
    for name in ("work_in_background", "schedule_task", "watch_for"):
        assert registry.get(name).request_suffices, name


def test_what_the_prompt_says_asks_first_really_asks(app):
    registry = assembly.get_registry()
    assert registry.get("control_computer").risk is Risk.HIGH
    assert not registry.get("write_file").request_suffices
    assert "write_file" in prompt.MAKING_ARTIFACTS and "only when they ask" in prompt.MAKING_ARTIFACTS


# --- the live check: it flags what it says it does ------------------------------------------------------------

GOOD = Reply(said="hi", text="Morning, Boss. What are we tackling first?")


def test_a_good_reply_is_not_flagged():
    assert reply_problems(GOOD) == []


@pytest.mark.parametrize("text,tools,problem", [
    ("Boss, it's 12. Anything else, Boss?", [], 'said "Boss" more than once'),
    ("Here you go:\n- one\n- two", [], "markdown"),
    ("It's **12**.", [], "markdown"),
    ("Certainly! It's 12.", [], "service phrasing"),
    ("How can I help you today?", [], "service phrasing"),
    ("I'll remind you at 9.", [], "promised a follow-up without setting one up"),
    ("I'll let you know when it's 5.", ["look_it_up"], "promised a follow-up"),
])
def test_each_problem_in_a_reply_is_flagged(text, tools, problem):
    found = reply_problems(Reply(said="x", text=text, tools=tools))
    assert any(problem in p for p in found), found


def test_a_promise_backed_by_a_real_tool_call_is_fine_and_a_failed_turn_is_reported():
    assert reply_problems(Reply(said="x", text="I'll remind you at 9.", tools=["schedule_task"])) == []
    assert reply_problems(Reply(said="x", text="", failed="no model")) == ["the turn failed: no model"]
    assert reply_problems(Reply(said="x", text="Ha.", laughs=2)) == ["laughed more than once in one reply"]


def scenario(*expect: str) -> Scenario:
    return Scenario("s", ("x",), expect=expect)


def test_each_conversation_expectation_is_flagged_and_passes_when_met():
    plain = [Reply(said="a", text="Twelve."), Reply(said="b", text="Eighteen."),
             Reply(said="c", text="Canberra.")]
    every = [Reply(said="a", text="Twelve, Boss."), Reply(said="b", text="Boss, eighteen."),
             Reply(said="c", text="Canberra, Boss.")]
    some = [plain[0], every[1], plain[2]]
    assert conversation_problems(scenario("boss_used"), plain) == [
        'never said "Boss" in a whole conversation']
    assert conversation_problems(scenario("boss_not_every_reply"), every) == [
        'said "Boss" in every single reply']
    assert conversation_problems(scenario("boss_used", "boss_not_every_reply"), some) == []

    nothing = [Reply(said="x", text="Sure, I'll sort that.")]
    assert conversation_problems(scenario("follow_up_tool_ran"), nothing)
    assert not conversation_problems(scenario("follow_up_tool_ran"),
                                     [Reply(said="x", text="Done.", tools=["schedule_task"])])
    assert conversation_problems(scenario("no_card"), [Reply(said="x", text="ok", cards=["schedule_task"])])
    assert conversation_problems(scenario("asked_first"), [Reply(said="x", text="Ran it: 4.")])
    assert not conversation_problems(scenario("asked_first"),
                                     [Reply(said="x", text="ok", cards=["run_code"])])
    assert not conversation_problems(scenario("asked_first"), [Reply(said="x", text="Want me to run it?")])
    assert conversation_problems(scenario("nothing_done_unasked"),
                                 [Reply(said="x", text="Noted.", tools=["remember_about_me"])])
    assert not conversation_problems(scenario("nothing_done_unasked"),
                                     [Reply(said="x", text="Want a reminder?", tools=["look_it_up"])])
    assert conversation_problems(scenario("no_laugh"), [Reply(said="x", text="Ha", laughs=1)])


# --- the live check never touches real data ---------------------------------------------------------------

def test_a_scratch_folder_overlapping_the_real_data_is_refused(tmp_path):
    real = tmp_path / "data"
    real.mkdir()
    assert not identity_check.is_safe_scratch(real, real)
    assert not identity_check.is_safe_scratch(real / "inside", real)
    assert not identity_check.is_safe_scratch(tmp_path, real)
    assert identity_check.is_safe_scratch(tmp_path / "elsewhere", real)
    with pytest.raises(SystemExit):
        identity_check.prepare_scratch(real, scratch=real / "inside")


def listing(folder: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in folder.rglob("*") if p.is_file()}


def test_the_scratch_copy_takes_only_model_settings_and_keys(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    for name in ("models.yaml", "models_state.json", "prefs.json", "jarvis.db", "notifications.json"):
        (real / name).write_text(f"real {name}", encoding="utf-8")
    env = tmp_path / "real.env"
    env.write_text("SOME_KEY=abc\n", encoding="utf-8")
    before = listing(real), env.read_text()

    monkeypatch.setenv("JARVIS_DATA_DIR", str(real))  # restored by monkeypatch afterwards
    monkeypatch.setenv("JARVIS_ENV_PATH", str(env))
    scratch = identity_check.prepare_scratch(real, scratch=tmp_path / "scratch", env_file=env)

    assert sorted(p.name for p in (scratch / "data").iterdir()) == [
        "models.yaml", "models_state.json", "prefs.json"]
    assert (scratch / ".env").read_text() == "SOME_KEY=abc\n"
    assert os.environ["JARVIS_DATA_DIR"] == str(scratch / "data")
    assert os.environ["JARVIS_ENV_PATH"] == str(scratch / ".env")
    assert (listing(real), env.read_text()) == before


def test_a_whole_run_through_the_real_turn_loop_reads_what_was_really_stored(app, tmp_path):
    """The check end to end, with a scripted model in place of the person's: the real turn loop,
    real tools, the real database. What it reports comes from the stored tool calls."""
    from session_scripted_model import SessionScriptedModel, install

    model = install(assembly, SessionScriptedModel())
    model.on("jarvis").calls_tool("schedule_task", {
        "title": "Call the bank", "when": "once", "time": "09:00", "action": "reminder",
        "text": "Call the bank"})
    model.on("jarvis").says("Done, Boss. I'll remind you at nine tomorrow.")   # backed by a real call
    model.on("jarvis").says("I'll let you know at five.")                     # nothing behind it
    model.on("teacher").says("Pay yourself first, Boss: save before you spend.")

    data_before = listing(Path(os.environ["JARVIS_DATA_DIR"]))
    report = identity_check.check([
        Scenario("ordinary", ("Remind me tomorrow at 9am to call the bank.",),
                 expect=("follow_up_tool_ran", "no_card")),
        Scenario("promise", ("Let me know at 5pm.",), expect=("follow_up_tool_ran",)),
        Scenario("direct", ("Teach me one thing about budgeting.",), agent="teacher"),
    ])
    ordinary, promise, direct = report["results"]

    assert ordinary["replies"][0]["tools"] == ["schedule_task"]
    assert ordinary["replies"][0]["cards"] == [] and ordinary["problems"] == []
    assert ordinary["replies"][0]["problems"] == []

    assert promise["replies"][0]["tools"] == []
    assert "promised a follow-up without setting one up" in promise["replies"][0]["problems"]
    assert promise["problems"] == ["nothing was actually scheduled or set going"]

    assert direct["replies"][0]["reply"].startswith("Pay yourself first, Boss")
    assert direct["problems"] == [] and direct["replies"][0]["problems"] == []
    assert report["flagged"] == 2
    assert "FLAG:   promised a follow-up without setting one up" in identity_check.render(report)
    # It wrote only where it was pointed (the scratch data dir of this test).
    assert set(listing(Path(os.environ["JARVIS_DATA_DIR"]))) >= set(data_before)


def test_the_command_asks_before_spending_anything(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(identity_check, "real_data_dir", lambda: tmp_path / "real")
    monkeypatch.setattr(identity_check, "prepare_scratch", lambda real: tmp_path / "scratch")
    monkeypatch.setattr(identity_check, "check", lambda *a, **k: calls.append(1))
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert identity_check.main([]) == 1
    assert calls == [] and "Nothing was run." in capsys.readouterr().out
