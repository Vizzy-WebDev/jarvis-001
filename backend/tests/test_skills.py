"""Folder Skills: instructions, and the two gates before anything runs.

A Skill is knowledge Jarvis does not already have — a process, a house style, a
template — never a rename of an ability it already has. Several of these tests
exist to keep that true structurally rather than by convention, because it is
the thing that has gone wrong repeatedly in the design this replaces.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from jarvis.capabilities import CapabilityKind, CapabilityRegistry, CapabilitySpec, Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.skills import capabilities as skill_capabilities
from jarvis.skills import files, install, pipelines
from jarvis.skills.templates import Unresolved, extract_refs, resolve


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


def _zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


SKILL_MD = """---
name: "weekly-report"
description: "How to write the weekly report."
allowed-tools: [get_time, read_web_page]
---

Open last week's notes and summarise them.
"""


# --- names --------------------------------------------------------------------

@pytest.mark.parametrize("raw,slug", [
    ("Weekly Report", "weekly-report"),
    ("  Deep   Clean  ", "deep-clean"),
    ("2024 planning", "s-2024-planning"),
    ("!!!", "skill"),
])
def test_a_name_becomes_a_valid_slug(raw, slug):
    assert files.slugify(raw) == slug


@pytest.mark.parametrize("bad", ["Weekly", "1abc", "has_underscore", "", "claude-helper",
                                 "anthropic-thing"])
def test_an_invalid_skill_name_is_refused(bad):
    with pytest.raises(ValueError):
        files.validate_name(bad)


def test_a_skill_cannot_take_the_name_of_a_real_ability():
    """A Skill called `get_time` would be silently answered by the wrong thing."""
    name = files.unique_name("get_time", {"get_time"})
    assert name != "get_time"


def test_a_second_skill_with_the_same_name_gets_its_own_folder():
    first = files.create_skill(name="Report", description="d", instructions="i")
    second = files.create_skill(name="Report", description="d", instructions="i")
    assert first["name"] == "report" and second["name"] == "report-2"


def test_a_description_is_required_because_it_is_how_the_model_knows_when_to_use_it():
    with pytest.raises(ValueError):
        files.create_skill(name="report", description="   ", instructions="i")


# --- frontmatter --------------------------------------------------------------

def test_frontmatter_and_body_are_split():
    parsed = files.parse_skill_md(SKILL_MD)
    assert parsed["frontmatter"]["name"] == "weekly-report"
    assert parsed["frontmatter"]["allowed-tools"] == ["get_time", "read_web_page"]
    assert parsed["body"].startswith("Open last week's notes")


def test_a_file_with_no_frontmatter_is_all_body_rather_than_an_error():
    parsed = files.parse_skill_md("Just do the thing.")
    assert parsed == {"frontmatter": {}, "body": "Just do the thing."}


def test_an_unclosed_fence_is_treated_as_plain_text_rather_than_half_parsed():
    text = "---\nname: x\nstill going"
    assert files.parse_skill_md(text)["body"] == text


def test_indented_lists_and_block_scalars_are_read():
    parsed = files.parse_skill_md(
        "---\n"
        "name: x\n"
        "tags:\n"
        "  - one\n"
        "  - two\n"
        "summary: |\n"
        "  first line\n"
        "  second line\n"
        "---\n\nbody")
    assert parsed["frontmatter"]["tags"] == ["one", "two"]
    assert parsed["frontmatter"]["summary"] == "first line\nsecond line"


def test_a_key_this_does_not_understand_is_left_alone_rather_than_raising():
    """A Skill written for another tool carries keys this never has to act on,
    and refusing to open it would be worse than ignoring them."""
    parsed = files.parse_skill_md("---\nname: x\ndescription: d\nlicense: MIT\n"
                                  "metadata:\n  nested: true\n---\nbody")
    assert parsed["frontmatter"]["license"] == "MIT"
    assert parsed["body"] == "body"


def test_writing_and_reading_a_skill_md_round_trips():
    written = files.serialize_skill_md(name="x", description="d", body="the body",
                                       extra={"allowed-tools": ["get_time"]})
    parsed = files.parse_skill_md(written)
    assert parsed["frontmatter"]["name"] == "x"
    assert parsed["frontmatter"]["allowed-tools"] == ["get_time"]
    assert parsed["body"] == "the body"


# --- staying inside the folder ------------------------------------------------

def test_reading_outside_a_skills_own_folder_is_refused():
    files.create_skill(name="report", description="d", instructions="i")
    for escape in ("../../.env", "/etc/passwd", "sub/../../../.env"):
        with pytest.raises(ValueError):
            files.resolve_skill_path("report", escape)


def test_an_archive_that_writes_outside_its_folder_is_refused_whole():
    hostile = _zip({"SKILL.md": SKILL_MD, "../../escaped.txt": "no"})
    with pytest.raises(install.InstallError) as raised:
        install.install_from_zip(hostile)
    assert "outside its own folder" in str(raised.value)


def test_a_file_too_big_to_paste_into_a_conversation_is_refused_with_its_size():
    skill = files.create_skill(name="report", description="d", instructions="i")
    big = files.skill_root(skill["name"]) / "huge.txt"
    big.write_text("x" * (files.MAX_FILE_BYTES + 10))
    with pytest.raises(ValueError) as raised:
        files.read_skill_file(skill["name"], "huge.txt")
    assert "too large" in str(raised.value)


# --- installing ---------------------------------------------------------------

def test_installing_a_zip_keeps_the_folder_name_authoritative():
    skill = install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "reference/style.md": "x"}))
    assert skill["name"] == "weekly-report"
    assert files.list_skill_files("weekly-report") == ["reference/style.md"]
    # Rewritten so the declared name can never disagree with where it lives.
    assert files.read_skill_md("weekly-report")["frontmatter"]["name"] == "weekly-report"


def test_a_pointless_wrapper_folder_is_seen_through():
    """A zip made by right-clicking a folder has one; so does every GitHub
    zipball."""
    skill = install.install_from_zip(_zip({"repo-main/SKILL.md": SKILL_MD}))
    assert skill["name"] == "weekly-report"


def test_an_archive_with_no_skill_md_is_refused_rather_than_guessed_at():
    with pytest.raises(install.InstallError) as raised:
        install.install_from_zip(_zip({"notes.txt": "hello"}))
    assert "SKILL.md" in str(raised.value)


def test_anything_installed_from_elsewhere_starts_with_both_gates_shut():
    skill = install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "skill.toml": ""}))
    assert skill["scriptsApproved"] is False
    assert skill["pipelineApproved"] is False


def test_a_skill_written_here_starts_with_its_own_pipeline_approved():
    """You wrote it and read it back — the question a gate would ask has already
    been answered."""
    skill = files.create_skill(name="report", description="d", instructions="i")
    assert skill["pipelineApproved"] is True
    assert skill["scriptsApproved"] is False, "scripts are a separate question"


def test_replacing_a_skills_files_resets_the_consent_that_was_given_for_the_old_ones():
    skill = install.install_from_zip(_zip({"SKILL.md": SKILL_MD}))
    files.update_skill_state(skill["name"], {"scriptsApproved": True,
                                             "pipelineApproved": True})
    install.replace_from_zip(skill["name"], _zip({"SKILL.md": SKILL_MD}))
    replaced = files.get_skill(skill["name"])
    assert replaced["scriptsApproved"] is False
    assert replaced["pipelineApproved"] is False


@pytest.mark.parametrize("link,owner_repo", [
    ("https://github.com/someone/a-skill", ("someone", "a-skill")),
    ("github.com/someone/a-skill.git", ("someone", "a-skill")),
    ("someone/a-skill", ("someone", "a-skill")),
    ("https://github.com/someone/a-skill/", ("someone", "a-skill")),
])
def test_the_shapes_of_repository_link_people_actually_paste(link, owner_repo):
    assert install.parse_github_repo(link) == owner_repo


def test_installing_from_a_repository_asks_for_its_real_default_branch():
    """Assuming `main` installs nothing from a repo still on `master`, with an
    error that blames the link."""
    asked = []

    class Response:
        def __init__(self, payload=None, content=b""):
            self._payload, self.content = payload, content

        def json(self):
            return self._payload

    def fetch(url):
        asked.append(url)
        if "api.github.com" in url:
            return Response({"default_branch": "master"})
        return Response(content=_zip({"repo-master/SKILL.md": SKILL_MD}))

    skill = install.install_from_github("someone/a-skill", fetch=fetch)
    assert skill["name"] == "weekly-report"
    assert skill["source"] == {"type": "github", "repo": "someone/a-skill",
                               "branch": "master"}
    assert any("refs/heads/master" in url for url in asked)


def test_a_repository_that_cannot_be_downloaded_says_so_plainly():
    def fetch(url):
        raise RuntimeError("404 Not Found")

    with pytest.raises(install.InstallError) as raised:
        install.install_from_github("someone/nope", fetch=fetch)
    assert "Couldn't download" in str(raised.value)


def test_a_skill_exports_as_a_zip_with_forward_slash_entries():
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "reference/style.md": "x"}))
    data = install.export_zip("weekly-report")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
    assert "weekly-report/reference/style.md" in names
    assert not any("\\" in name for name in names)


# --- becoming something the model can call ------------------------------------

def _registry_with_tools() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        id="builtin.get_time", name="get_time", description="the time",
        input_schema={"type": "object", "properties": {}}, risk=Risk.LOW,
        handler=lambda: {"iso": "now"}))
    registry.register(CapabilitySpec(
        id="builtin.send_message", name="send_message", description="send",
        input_schema={"type": "object", "properties": {}}, risk=Risk.HIGH,
        handler=lambda **_: "sent"))
    return registry


def test_an_enabled_skill_becomes_a_callable_capability():
    files.create_skill(name="report", description="How to write the report.",
                       instructions="Open the notes.")
    registry = _registry_with_tools()
    assert skill_capabilities.sync(registry) == ["report"]

    spec = registry.get("report")
    assert spec.kind is CapabilityKind.SKILL
    assert spec.description == "How to write the report."
    assert spec.handler(ctx=None) == {"ok": True, "instructions": "Open the notes."}


def test_disabling_a_skill_takes_it_out_of_what_the_model_can_call():
    files.create_skill(name="report", description="d", instructions="i")
    registry = _registry_with_tools()
    skill_capabilities.sync(registry)
    files.update_skill_state("report", {"enabled": False})
    assert skill_capabilities.sync(registry) == []
    assert registry.get("report") is None


def test_a_deleted_skill_stops_being_callable():
    """A model calling something that no longer exists is worse than it not
    being offered."""
    files.create_skill(name="report", description="d", instructions="i")
    registry = _registry_with_tools()
    skill_capabilities.sync(registry)
    files.delete_skill("report")
    skill_capabilities.sync(registry)
    assert registry.get("report") is None


def test_calling_a_skill_lists_its_files_and_says_they_can_only_be_read():
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "reference/style.md": "x"}))
    registry = _registry_with_tools()
    skill_capabilities.sync(registry)
    result = registry.get("weekly-report").handler(ctx=None)
    assert "reference/style.md" in result["instructions"]
    assert "never run them" in result["instructions"]
    # allowed-tools is communicated, not enforced by filtering mid-turn.
    assert "get_time, read_web_page" in result["instructions"]


def test_the_declaration_list_never_reads_the_instructions_body(monkeypatch):
    """Progressive disclosure: the body is read when a Skill is CALLED, not to
    build a declaration for every turn."""
    files.create_skill(name="report", description="d", instructions="the body")
    reads = {"count": 0}
    real = files.read_skill_body

    def counted(name):
        reads["count"] += 1
        return real(name)

    monkeypatch.setattr(files, "read_skill_body", counted)
    monkeypatch.setattr("jarvis.skills.capabilities.files.read_skill_body", counted)

    registry = _registry_with_tools()
    skill_capabilities.sync(registry)
    assert reads["count"] == 0
    registry.get("report").handler(ctx=None)
    assert reads["count"] == 1


# --- pipelines ----------------------------------------------------------------

PIPELINE = """
description = "Check the time then say something."

[[inputs]]
name = "topic"
type = "string"
required = true

[[steps]]
id = "now"
tool = "get_time"

[[steps]]
id = "say"
prompt = "It is {{steps.now.iso}} and the topic is {{inputs.topic}}."
"""


def test_a_pipeline_parses_into_plain_data():
    parsed = pipelines.parse(PIPELINE)
    assert parsed.description == "Check the time then say something."
    assert [s["id"] for s in parsed.steps] == ["now", "say"]
    assert parsed.inputs[0]["name"] == "topic"


def test_broken_toml_is_a_named_error_not_a_silent_misreading():
    with pytest.raises(ValueError) as raised:
        pipelines.parse("[[steps]\nid = ")
    assert "skill.toml could not be read" in str(raised.value)


@pytest.mark.parametrize("source,problem", [
    ('[[steps]]\nid = "a"\ntool = "get_time"\n[[steps]]\nid = "a"\nprompt = "x"',
     "duplicate id"),
    ('[[steps]]\nid = "a"\ntool = "get_time"\nprompt = "x"', "both"),
    ('[[steps]]\nid = "a"', "neither"),
    ('[[steps]]\nid = "a"\ntool = "nope"', "unknown tool"),
    ('[[steps]]\nid = "a"\nprompt = "{{inputs.missing}}"', "no input named"),
    ('[[steps]]\nid = "a"\nprompt = "{{steps.later}}"\n[[steps]]\nid = "later"\ntool = "get_time"',
     "has not run yet"),
    ('[[steps]]\nid = "a"\nprompt = "{{nonsense.x}}"', "not a valid reference"),
])
def test_a_pipeline_that_would_run_wrong_is_refused_with_the_reason(source, problem):
    errors = pipelines.validate(pipelines.parse(source),
                                known_tools={"get_time", "send_message"})
    assert any(problem in error for error in errors), errors


def test_a_valid_pipeline_has_no_complaints():
    assert pipelines.validate(pipelines.parse(PIPELINE),
                              known_tools={"get_time"}) == []


def test_references_keep_their_real_type_when_they_are_the_whole_value():
    context = {"inputs": {"n": 3}, "steps": {"a": {"ok": True, "result": {"items": [1, 2]}}}}
    assert resolve("{{steps.a.items}}", context) == [1, 2]
    assert resolve("there are {{inputs.n}} of them", context) == "there are 3 of them"


def test_an_unresolved_reference_raises_rather_than_becoming_blank():
    """A step running with a silently blank argument produces a plausible result
    built on nothing, which is worse than stopping."""
    with pytest.raises(Unresolved):
        resolve("{{steps.never.ran}}", {"inputs": {}, "steps": {}})


def test_a_failed_step_is_not_visible_to_a_later_one():
    context = {"inputs": {}, "steps": {"a": {"ok": False, "result": "half a thing"}}}
    with pytest.raises(Unresolved):
        resolve("{{steps.a}}", context)


def test_extracting_every_reference_for_validation():
    found = extract_refs({"a": "{{inputs.x}}", "b": ["{{steps.y.z}}"]})
    assert {(r.root, r.id) for r in found} == {("inputs", "x"), ("steps", "y")}


def test_a_pipeline_runs_its_steps_in_order_and_feeds_one_into_the_next():
    ran = []

    def invoke(name, args):
        ran.append((name, args))
        return {"iso": "09:00"}

    result = pipelines.run(pipelines.parse(PIPELINE), invoke=invoke,
                           ask=lambda prompt: f"said: {prompt}",
                           inputs={"topic": "the roof"})
    assert result["ok"] is True
    assert ran == [("get_time", {})]
    assert result["steps"][1]["result"] == "said: It is 09:00 and the topic is the roof."


def test_a_pipeline_stops_at_the_first_failure_rather_than_running_on():
    def invoke(name, args):
        raise RuntimeError("the tool refused")

    result = pipelines.run(pipelines.parse(PIPELINE), invoke=invoke,
                           ask=lambda prompt: "unused", inputs={"topic": "x"})
    assert result["ok"] is False
    assert result["error"] == "the tool refused"
    assert len(result["steps"]) == 1, "the second step must not run on a missing value"


def test_a_pipeline_that_arrived_from_elsewhere_does_not_run_until_approved():
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "skill.toml": PIPELINE}))
    registry = _registry_with_tools()
    skill_capabilities.sync(registry)

    result = registry.get("weekly-report").handler(ctx=None)
    assert result["pipelineNeedsApproval"] is True
    assert "approve_skill_pipeline" in result["instructions"]


def test_an_approved_pipeline_declares_its_inputs_as_arguments():
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "skill.toml": PIPELINE}))
    files.update_skill_state("weekly-report", {"pipelineApproved": True})
    registry = _registry_with_tools()
    skill_capabilities.sync(registry)

    schema = registry.get("weekly-report").input_schema
    assert schema["properties"]["topic"]["type"] == "string"
    assert schema["required"] == ["topic"]


def test_a_missing_required_input_is_caught_before_any_step_runs():
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "skill.toml": PIPELINE}))
    files.update_skill_state("weekly-report", {"pipelineApproved": True})
    registry = _registry_with_tools()
    skill_capabilities.sync(registry)

    result = registry.get("weekly-report").handler(ctx=None)
    assert result["ok"] is False
    assert 'Missing required input: "topic"' in result["error"]


def test_a_pipeline_with_a_confirm_worthy_step_makes_the_whole_skill_confirm():
    """Once, up front, naming the step — rather than pausing halfway through."""
    risky = '[[steps]]\nid = "send"\ntool = "send_message"\n'
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "skill.toml": risky}))
    files.update_skill_state("weekly-report", {"pipelineApproved": True})
    registry = _registry_with_tools()
    skill_capabilities.sync(registry)

    spec = registry.get("weekly-report")
    assert spec.risk is Risk.MEDIUM
    assert "send_message" in spec.summarize({})


def test_a_broken_pipeline_never_breaks_the_declaration():
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "skill.toml": "[[steps]\nnope"}))
    registry = _registry_with_tools()
    assert skill_capabilities.sync(registry) == ["weekly-report"]
    result = registry.get("weekly-report").handler(ctx=None)
    # Falls back to instructions, and the parse error is not spliced into them.
    assert result["ok"] is True
    assert "nope" not in result["instructions"]


# --- the tools ----------------------------------------------------------------

def _tools(registry):
    from jarvis.tools.skill_tools import build

    return {spec.name: spec for spec in build(registry)}


def test_creating_a_skill_makes_it_immediately_callable():
    registry = _registry_with_tools()
    tools = _tools(registry)
    answer = tools["create_skill"].handler(name="Deep Clean", description="How to deep clean.",
                                           instructions="Start at the top.")
    assert answer["ok"] is True and answer["name"] == "deep-clean"
    assert registry.get("deep-clean") is not None


def test_creating_a_skill_is_read_back_before_it_happens():
    registry = _registry_with_tools()
    spec = _tools(registry)["create_skill"]
    assert spec.risk is Risk.MEDIUM
    assert "Deep clean" in spec.summarize({"name": "Deep clean", "description": "x"})


def test_a_script_will_not_run_until_that_skill_has_been_approved_once():
    registry = _registry_with_tools()
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD,
                                   "scripts/go.py": "print('ran')"}))
    tools = _tools(registry)

    refused = tools["run_skill_script"].handler(skill="weekly-report", path="scripts/go.py")
    assert refused["ok"] is False and refused["needsApproval"] is True
    assert "approve_skill_scripts" in refused["error"]

    tools["approve_skill_scripts"].handler(skill="weekly-report")
    allowed = tools["run_skill_script"].handler(skill="weekly-report", path="scripts/go.py")
    assert allowed["ok"] is True and allowed["stdout"].strip() == "ran"


def test_approving_scripts_is_asked_once_not_per_run():
    registry = _registry_with_tools()
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "scripts/go.py": "print('ran')"}))
    tools = _tools(registry)
    tools["approve_skill_scripts"].handler(skill="weekly-report")
    for _ in range(3):
        assert tools["run_skill_script"].handler(skill="weekly-report",
                                                 path="scripts/go.py")["ok"] is True


def test_a_file_that_is_not_a_script_is_refused_by_the_runner_but_still_readable():
    registry = _registry_with_tools()
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "reference/style.md": "# Style"}))
    files.update_skill_state("weekly-report", {"scriptsApproved": True})
    tools = _tools(registry)

    refused = tools["run_skill_script"].handler(skill="weekly-report",
                                                path="reference/style.md")
    assert refused["ok"] is False and "read_skill_file" in refused["error"]

    read = tools["read_skill_file"].handler(skill="weekly-report", path="reference/style.md")
    assert read["ok"] is True and read["content"] == "# Style"


def test_reading_outside_a_skill_folder_through_the_tool_is_refused():
    registry = _registry_with_tools()
    files.create_skill(name="report", description="d", instructions="i")
    answer = _tools(registry)["read_skill_file"].handler(skill="report", path="../../.env")
    assert answer["ok"] is False and "outside" in answer["error"]


def test_approving_a_pipeline_changes_what_the_model_is_offered():
    registry = _registry_with_tools()
    install.install_from_zip(_zip({"SKILL.md": SKILL_MD, "skill.toml": PIPELINE}))
    skill_capabilities.sync(registry)
    assert registry.get("weekly-report").input_schema["properties"] == {}

    _tools(registry)["approve_skill_pipeline"].handler(skill="weekly-report")
    assert "topic" in registry.get("weekly-report").input_schema["properties"]


def test_approving_a_pipeline_on_a_skill_that_has_none_says_so():
    registry = _registry_with_tools()
    files.create_skill(name="report", description="d", instructions="i")
    answer = _tools(registry)["approve_skill_pipeline"].handler(skill="report")
    assert answer["ok"] is False and "doesn't have a pipeline" in answer["error"]


# --- the routes ---------------------------------------------------------------

def test_the_routes_only_ever_show_folder_skills(live_server):
    """The permanent rule: a built-in ability must never appear as a Skill. This
    is structural — the route reads the folder list, which cannot return one."""
    import httpx

    files.create_skill(name="report", description="How to write it.", instructions="i")
    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        listed = client.get("/api/skills/installed").json()
        detail = client.get("/api/skills/report").json()
        missing = client.get("/api/skills/nope")

    assert [s["name"] for s in listed["skills"]] == ["report"]
    assert detail["body"] == "i"
    assert missing.status_code == 404


def test_a_skill_can_be_installed_edited_and_removed_over_http(live_server):
    import httpx

    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        created = client.post("/api/skills", json={"name": "Deep Clean",
                                                   "description": "How to deep clean.",
                                                   "instructions": "Start at the top."})
        name = created.json()["skill"]["name"]

        client.patch(f"/api/skills/{name}", json={"enabled": False})
        assert client.get(f"/api/skills/{name}").json()["enabled"] is False

        downloaded = client.get(f"/api/skills/{name}/download")
        assert downloaded.headers["content-disposition"].startswith("attachment")

        assert client.delete(f"/api/skills/{name}").json()["ok"] is True
        assert client.get(f"/api/skills/{name}").status_code == 404


def test_installing_a_zip_over_http(live_server):
    import httpx

    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        installed = client.post("/api/skills/install",
                                content=_zip({"SKILL.md": SKILL_MD}),
                                headers={"Content-Type": "application/zip"})
    assert installed.json()["skill"]["name"] == "weekly-report"
