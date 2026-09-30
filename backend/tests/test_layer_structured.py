"""Structured output — native, emulated with validation and repair, buffered when
streamed — and prompt profiles."""

from __future__ import annotations

import pytest

from jarvis import models
from jarvis.models import errors, execute
from jarvis.models.drivers import fake
from jarvis.models.types import Done, OutputSpec, Section, TextDelta, Tool

from layer_helpers import ask, configure, fake_conn, layer  # noqa: F401

SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"],
          "additionalProperties": False}
JSON_ONLY = {"m": {"capabilities": {"text_in": True, "json_mode": True}}}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)


def test_a_required_schema_on_a_json_mode_only_model_fails_clearly(layer):
    configure(layer, [fake_conn("jm", models=JSON_ONLY)])
    with pytest.raises(errors.NoEligibleEndpoint) as caught:
        models.generate(ask(output=OutputSpec("json", SCHEMA, "required")))
    assert "structured_output_strict" in str(caught.value) or "enforce" in str(caught.value)
    assert fake.calls() == []


def test_best_effort_on_that_model_is_emulated_and_marked(layer):
    configure(layer, [fake_conn("jm", models=JSON_ONLY)])
    fake.queue("jm", fake.reply('{"answer": 5}'))
    response = models.generate(ask(output=OutputSpec("json", SCHEMA, "best_effort")))
    assert response.data == {"answer": 5}
    assert response.report.features["structured_output"] == "emulated"
    prepared = fake.calls("jm")[0].prepared
    assert prepared.output.mode == "json_mode"
    assert any(s.label == "output_format" and '"answer"' in s.text for s in prepared.system)


def test_an_invalid_emulated_reply_is_repaired_with_the_validation_errors(layer):
    configure(layer, [fake_conn("jm", models=JSON_ONLY)])
    fake.queue("jm", fake.reply('{"answer": "five"}'), fake.reply('{"answer": 5}'))
    response = models.generate(ask(output=OutputSpec("json", SCHEMA, "best_effort")))
    assert response.data == {"answer": 5}
    repair = fake.calls("jm")[1].prepared.items
    assert repair[-1].text.startswith("Your reply didn't match the required JSON Schema: answer:")
    assert repair[-2].text == '{"answer": "five"}'  # its own reply, unchanged, before the correction
    assert any("correction" in w for w in response.report.warnings)
    assert response.usage.input == 20  # both calls count


def test_repair_that_never_validates_fails_with_schema_validation_failed(layer):
    configure(layer, [fake_conn("jm", models=JSON_ONLY), fake_conn("other", models=JSON_ONLY)],
              settings={"repair_attempts": 2})
    fake.queue("jm", fake.reply("nope"), fake.reply("still no"), fake.reply("never"))
    with pytest.raises(errors.SchemaValidationFailed):
        models.generate(ask(output=OutputSpec("json", SCHEMA, "best_effort")))
    assert len(fake.calls("jm")) == 3 and fake.calls("other") == []  # not retryable: no fallback


def test_emulated_output_is_buffered_until_it_validates_when_streamed(layer):
    configure(layer, [fake_conn("jm", models=JSON_ONLY)])
    fake.queue("jm", fake.reply('{"answer": "x"}', chunks=4), fake.reply('{"answer": 7}', chunks=4))
    events = list(models.stream(ask(output=OutputSpec("json", SCHEMA, "best_effort"))))
    texts = [e for e in events if isinstance(e, TextDelta)]
    assert [t.text for t in texts] == ['{"answer": 7}']  # nothing of the invalid first try ever reached the caller
    assert isinstance(events[-1], Done) and events[-1].response.data == {"answer": 7}


def test_a_native_strict_endpoint_is_preferred_as_native_and_validated(layer):
    configure(layer, [fake_conn("strict", models={"m": {"capabilities": {"text_in": True,
                                                                          "structured_output_strict": True}}})])
    fake.queue("strict", fake.reply('{"answer": "not a number"}'))
    with pytest.raises(errors.SchemaValidationFailed, match="meant to enforce"):
        models.generate(ask(output=OutputSpec("json", SCHEMA, "best_effort")))


def test_a_code_fence_is_looked_past_for_parsing_but_the_reply_is_not_changed(layer):
    configure(layer, [fake_conn("jm", models=JSON_ONLY)])
    fake.queue("jm", fake.reply('```json\n{"answer": 3}\n```'))
    response = models.generate(ask(output=OutputSpec("json", SCHEMA, "best_effort")))
    assert response.data == {"answer": 3} and response.text == '```json\n{"answer": 3}\n```'


def test_prompt_profiles_render_sections_per_family(layer):
    configure(layer, [fake_conn("x", models={"m": {"family": "tagged", "capabilities": {"text_in": True,
                                                                                        "tools": True}}}),
                      fake_conn("y", models={"m": {"family": "plain", "capabilities": {"text_in": True,
                                                                                       "tools": True}}})],
              prompt_profiles={"tagged": {"section_format": "xml-tags", "tool_guidance": "Call one tool at a time."}},
              aliases={"x": {"endpoint": "x/m"}, "y": {"endpoint": "y/m"}})
    from jarvis.models.types import Requirements

    request = ask(instructions=(Section("persona", "Be kind."), Section("house rules", "No swearing.")),
                  tools=(Tool("t", "", {"type": "object"}),))
    models.generate(type(request)(**{**request.__dict__, "requirements": Requirements(pin="x")}))
    models.generate(type(request)(**{**request.__dict__, "requirements": Requirements(pin="y")}))
    tagged = [s.text for s in fake.calls("x")[0].prepared.system]
    plain = [s.text for s in fake.calls("y")[0].prepared.system]
    assert tagged == ["<persona>\nBe kind.\n</persona>", "<house_rules>\nNo swearing.\n</house_rules>",
                      "<tool_use>\nCall one tool at a time.\n</tool_use>"]
    assert plain == ["## Persona\n\nBe kind.", "## House rules\n\nNo swearing."]


def test_the_stable_prefix_is_marked_only_up_to_its_section(layer):
    configure(layer, [fake_conn("c", models={"m": {"capabilities": {"text_in": True, "prompt_caching": True}}})])
    from jarvis.models.types import Hints

    models.generate(ask(instructions=(Section("a", "1"), Section("b", "2"), Section("c", "3")),
                        hints=Hints(stable_prefix_until="b")))
    prepared = fake.calls("c")[0].prepared
    assert [s.stable for s in prepared.system] == [True, True, False] and prepared.cache
