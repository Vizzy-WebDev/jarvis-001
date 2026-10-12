"""How Jarvis picks among connected apps: by what was asked, and by what the person said.

Found by replaying `find_capability` over the real connector tool lists (Oct 2026):
- "generate an image ... but do not use higgsfield" still returned six Higgsfield tools, and
  "use the github connector not composio" returned Composio first: a NAMED service counted as a
  positive match even when it was named to be left out.
- "create a github issue" returned Higgsfield's `create_*` tools: a verb in 30 tool names
  outweighed the one distinctive word, because every word counted the same.
Nothing here names a service in code; the tests use made-up apps with the same shape.
"""

from __future__ import annotations

import pytest

from jarvis.capabilities import CapabilityKind, CapabilityRegistry, CapabilitySpec, Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.tools.find_capability import build as build_find


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


def _tool(registry, name, description):
    registry.register(CapabilitySpec(
        id=f"connector.{name}", name=name, description=description,
        input_schema={"type": "object", "properties": {}}, risk=Risk.LOW, handler=lambda: {},
        kind=CapabilityKind.CONNECTOR))


@pytest.fixture
def registry():
    reg = CapabilityRegistry()
    # a generator app with many tools, most of them "create_..." / "generate_..."
    for n in ("generate_image", "generate_video", "generate_audio", "create_voice", "create_website",
              "create_project", "create_scene", "create_clip", "create_preview", "create_batch"):
        _tool(reg, f"pixelforge__{n}", f"Pixelforge: {n.replace('_', ' ')} from a prompt.")
    # a second generator
    _tool(reg, "artisan__generate_image", "Artisan: generate an image from a prompt.")
    # an aggregator whose one description lists many apps
    _tool(reg, "hubwire__search_tools",
          "Connects 500+ apps: Slack, Gitlab, Notion, Sheets. Search tools for the user's query.")
    _tool(reg, "gitforge__create_issue", "Gitforge: create an issue in a repository.")
    _tool(reg, "notebook__create_pages", "Notebook: create pages.")
    return reg


def _find(registry, intent, **extra):
    [spec] = build_find(registry)
    return spec.handler(intent=intent, **extra)


def _names(answer):
    return [f["name"] for f in answer["found"]]


def test_a_distinctive_word_is_worth_more_than_a_verb_every_tool_shares(registry):
    from jarvis.tools.find_capability import _weights

    specs = registry.list()
    weights = _weights(specs, ["create", "sheets"])
    # "create" is in a dozen tool names, "sheets" in one description: the rare word must
    # outweigh the common one even when the common one is matched in a NAME (x3).
    assert weights["sheets"] * 1 > weights["create"] * 3


def test_an_app_named_to_be_left_out_is_left_out(registry):
    answer = _find(registry, "generate an image but do not use pixelforge")
    names = _names(answer)
    assert names and all(not n.startswith("pixelforge__") for n in names)
    assert "artisan__generate_image" in names
    assert "pixelforge" in answer["note"].lower()


@pytest.mark.parametrize("phrase", [
    "generate an image, don't use pixelforge",
    "generate an image without pixelforge",
    "generate an image instead of pixelforge",
    "generate an image with anything other than pixelforge",
    "generate an image not through pixelforge",
])
def test_the_usual_ways_of_saying_not_are_understood(registry, phrase):
    assert all(not n.startswith("pixelforge__") for n in _names(_find(registry, phrase)))


def test_the_model_can_pass_the_instruction_as_a_real_argument(registry):
    """A rule stated only in prose cannot be followed by a model that sticks to its schema."""
    answer = _find(registry, "generate an image", avoid=["pixelforge"])
    assert all(not n.startswith("pixelforge__") for n in _names(answer))
    [spec] = build_find(registry)
    props = spec.input_schema["properties"]
    assert props["avoid"]["type"] == "array" and props["avoid"]["items"] == {"type": "string"}
    assert props["prefer"]["type"] == "string"


def test_an_app_asked_for_by_name_comes_first(registry):
    names = _names(_find(registry, "generate an image", prefer="artisan"))
    assert names[0] == "artisan__generate_image"
    names = _names(_find(registry, "generate an image with artisan"))
    assert names[0] == "artisan__generate_image"


def test_leaving_out_the_aggregator_when_asked_to_use_the_apps_own_connector(registry):
    names = _names(_find(registry, "create a gitforge issue, not through hubwire"))
    assert names[0] == "gitforge__create_issue" and not any(n.startswith("hubwire__") for n in names)


def test_leaving_out_the_only_app_that_can_do_it_says_so_instead_of_substituting(registry):
    answer = _find(registry, "create an issue but not gitforge")
    assert all(not n.startswith("gitforge__") for n in _names(answer))


def test_without_an_instruction_nothing_changes(registry):
    assert _names(_find(registry, "generate an image"))[0].endswith("generate_image")


# --- choosing among apps that can all do the job, and a provider or model named by the person -------

@pytest.fixture
def generators():
    """Two generators; one has many more tools, so a plain top-6 is all that one."""
    reg = CapabilityRegistry()
    for n in ("generate_image", "generate_image_batch", "generate_image_edit", "generate_image_variant",
              "generate_image_upscale", "generate_image_inpaint", "generate_image_outpaint",
              "generate_image_style", "generate_image_remix"):
        _tool(reg, f"pixelforge__{n}", f"Pixelforge: {n.replace('_', ' ')} from a prompt.")
    reg.register(CapabilitySpec(
        id="connector.artisan.make", name="artisan__make_picture",
        description="Artisan: generate an image from a prompt.",
        input_schema={"type": "object", "properties": {"engine": {
            "type": "string", "description": "One of: fluxpro, sketchlite."}}},
        risk=Risk.LOW, handler=lambda: {}, kind=CapabilityKind.CONNECTOR))
    return reg


def test_every_app_that_can_do_the_job_is_in_the_answer_not_just_the_one_with_most_tools(generators):
    answer = _find(generators, "generate an image")
    assert {n.split("__")[0] for n in _names(answer)} == {"pixelforge", "artisan"}
    assert answer["options"] == ["artisan", "pixelforge"]
    assert "more than one" in answer["note"].lower()


def test_a_model_named_inside_an_apps_own_tool_points_at_that_app(generators):
    answer = _find(generators, "generate an image", prefer="fluxpro")
    assert _names(answer)[0] == "artisan__make_picture"
    assert "artisan" in answer["note"].lower() and "fluxpro" in answer["note"].lower()
    assert "unmatched" not in answer


def test_a_provider_nothing_here_offers_is_not_silently_replaced(generators):
    answer = _find(generators, "generate an image", prefer="midjourney")
    assert answer["unmatched"] == ["midjourney"]
    note = answer["note"].lower()
    assert "midjourney" in note and "no connected app is named" in note
    assert "ask" in note and "say so" in note
    # the candidates are still reachable, so its own model list can be checked
    assert _names(answer)


def test_naming_an_app_that_exists_is_not_called_unmatched(generators):
    answer = _find(generators, "generate an image", prefer="artisan")
    assert "unmatched" not in answer and _names(answer)[0].startswith("artisan__")


def test_a_single_capable_app_adds_no_options_note(registry):
    answer = _find(registry, "create pages in notebook")
    assert "options" not in answer


# --- the person's own words reach the router, whatever the model chose to pass -------------------------

def _say(session, text):
    from jarvis import conversation

    conversation.load_snapshot(session, [{"role": "user", "text": text}])


def _ctx(session="s-words"):
    from jarvis.policy import Autonomy, CallContext, Surface

    return CallContext(session_id=session, turn_id="t1", surface=Surface.TEXT, autonomy=Autonomy.INTERACTIVE)


def test_an_exclusion_in_the_persons_message_applies_even_if_the_model_left_it_out(generators):
    """Seen live: told "use X, not Y or Z", the model searched with just "generate a video with X"."""
    _say("s-words", "Generate an image of a car. Do not use Pixelforge.")
    [spec] = build_find(generators)
    answer = spec.handler(intent="generate an image of a car", ctx=_ctx("s-words"))
    assert all(not n["name"].startswith("pixelforge__") for n in answer["found"])
    assert any(n["name"].startswith("artisan__") for n in answer["found"])


def test_an_app_named_in_the_persons_message_comes_first_even_if_the_model_did_not_say_so(generators):
    _say("s-words2", "Use Artisan to make me a picture.")
    [spec] = build_find(generators)
    answer = spec.handler(intent="make a picture", ctx=_ctx("s-words2"))
    assert answer["found"][0]["name"].startswith("artisan__")


def test_with_no_conversation_behind_it_nothing_changes(generators):
    [spec] = build_find(generators)
    assert spec.handler(intent="generate an image", ctx=_ctx("nobody-said-anything"))["found"]
    assert spec.handler(intent="generate an image", ctx=None)["found"]


def test_when_several_apps_can_do_it_the_model_is_told_to_use_one(generators):
    note = _find(generators, "generate an image")["note"].lower()
    assert "exactly one" in note and "unless the user asked" in note
