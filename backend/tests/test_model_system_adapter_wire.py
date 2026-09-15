"""Pure unit tests of each adapter's message translation — no network."""

from jarvis.model_system.adapters import anthropic, gemini, openai_compatible
from jarvis.model_system.request import Attachment, Message, Modality, ToolCall


def test_anthropic_replays_raw_content_verbatim_when_present():
    raw_content = [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "1",
                                                     "name": "x", "input": {}}]
    messages = (Message(role="assistant", raw={"adapter": "anthropic", "content": raw_content}),)
    wire = anthropic.to_wire(messages)
    assert wire == [{"role": "assistant", "content": raw_content}]


def test_anthropic_falls_back_to_tool_calls_with_no_raw():
    messages = (Message(role="assistant", text="ok",
                        tool_calls=(ToolCall("1", "search", {"q": "x"}),)),)
    wire = anthropic.to_wire(messages)
    assert wire[0]["content"][0] == {"type": "text", "text": "ok"}
    assert wire[0]["content"][1]["type"] == "tool_use"
    assert wire[0]["content"][1]["name"] == "search"


def test_anthropic_ignores_raw_from_a_different_adapter():
    messages = (Message(role="assistant", text="plain",
                        raw={"adapter": "gemini", "content": {"parts": []}}),)
    wire = anthropic.to_wire(messages)
    assert wire == [{"role": "assistant", "content": "plain"}]


def test_anthropic_tool_result_message_is_not_double_encoded():
    messages = (Message(role="tool", tool_call_id="1", text='{"ok": true}'),)
    wire = anthropic.to_wire(messages)
    assert wire[0]["content"][0]["type"] == "tool_result"
    assert wire[0]["content"][0]["tool_use_id"] == "1"
    assert wire[0]["content"][0]["content"] == '{"ok": true}'  # not re-json.dumps'd


def test_anthropic_image_attachment_becomes_a_base64_block():
    messages = (Message(role="user", text="what is this",
                        attachments=(Attachment(Modality.IMAGE, "image/png", data=b"abc"),)),)
    wire = anthropic.to_wire(messages)
    assert wire[0]["content"][1]["type"] == "image"
    assert wire[0]["content"][1]["source"]["media_type"] == "image/png"


def test_gemini_replays_raw_content_for_thought_signature():
    raw_content = {"role": "model", "parts": [{"text": "hi"}], "thoughtSignature": "sig"}
    messages = (Message(role="assistant", raw={"adapter": "gemini", "content": raw_content}),)
    wire = gemini.to_wire(messages)
    assert wire == [raw_content]


def test_gemini_falls_back_to_function_call_parts():
    messages = (Message(role="assistant", tool_calls=(ToolCall("1", "search", {"q": "x"}),)),)
    wire = gemini.to_wire(messages)
    assert wire[0]["parts"][0]["functionCall"]["name"] == "search"


def test_gemini_tool_result_correlates_by_name_not_call_id():
    messages = (Message(role="tool", tool_call_id="call_1", tool_name="search",
                        text='{"result": 42}'),)
    wire = gemini.to_wire(messages)
    response = wire[0]["parts"][0]["functionResponse"]
    assert response["name"] == "search"
    assert response["response"] == {"result": 42}


def test_gemini_tool_result_wraps_a_non_dict_result():
    messages = (Message(role="tool", tool_call_id="call_1", tool_name="search", text="42"),)
    wire = gemini.to_wire(messages)
    assert wire[0]["parts"][0]["functionResponse"]["response"] == {"result": 42}


def test_gemini_thinking_config_tiers_vs_budget():
    from jarvis.model_system.reasoning import Effort, ReasoningKind, ReasoningRequest, ReasoningScheme

    tiers_scheme = ReasoningScheme(kind=ReasoningKind.TIERS, levels=(Effort.LOW,),
                                   default=Effort.LOW, native={Effort.LOW: "LOW"})
    tiers_req = ReasoningRequest(level=Effort.LOW, scheme=tiers_scheme, requested=Effort.LOW,
                                 clamped=False)
    assert gemini._thinking_config(tiers_req) == {"thinkingLevel": "LOW"}

    budget_scheme = ReasoningScheme(kind=ReasoningKind.BUDGET, levels=(Effort.LOW,),
                                    default=Effort.LOW, native={Effort.LOW: 4096})
    budget_req = ReasoningRequest(level=Effort.LOW, scheme=budget_scheme, requested=Effort.LOW,
                                  clamped=False)
    assert gemini._thinking_config(budget_req) == {"thinkingBudget": 4096}


def test_openai_compatible_tool_call_message_shape():
    messages = (Message(role="assistant", text="ok",
                        tool_calls=(ToolCall("1", "search", {"q": "x"}),)),)
    wire = openai_compatible.to_wire(messages, system="sys")
    assert wire[0] == {"role": "system", "content": "sys"}
    assert wire[1]["tool_calls"][0]["function"]["name"] == "search"


def test_openai_compatible_tool_result_message():
    messages = (Message(role="tool", tool_call_id="1", text="42"),)
    wire = openai_compatible.to_wire(messages, system="")
    assert wire[1] == {"role": "tool", "tool_call_id": "1", "content": "42"}


def test_openai_compatible_drops_video_attachments():
    messages = (Message(role="user", text="watch this",
                        attachments=(Attachment(Modality.VIDEO, "video/mp4", uri="x"),)),)
    wire = openai_compatible.to_wire(messages, system="")
    assert wire[1]["content"] == [{"type": "text", "text": "watch this"}]
