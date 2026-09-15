"""Gemini — the generateContent wire format.

Two details that must be right or the next call is rejected outright,
confirmed against the installed SDK rather than remembered:

* **`thought_signature` must round-trip verbatim.** The model's own
  `candidates[0].content` is replayed unchanged via `Message.raw`, never a
  hand-rebuilt `{role, parts}` object.
* **Streamed chunks are incremental deltas, not cumulative** — text is
  concatenated across chunks — while `usage_metadata` on each chunk is the
  CUMULATIVE total for the turn, so the last one seen is kept, never summed.

`ThinkingConfig` takes `thinking_level` (a `ThinkingLevel` — MINIMAL/LOW/
MEDIUM/HIGH, no OFF, no rung above HIGH) or `thinking_budget` (an integer);
this wire can carry either shape, decided by which the model's own
`ReasoningScheme.kind` declares.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from ..reasoning import ReasoningKind, ReasoningRequest
from ..request import (
    Completed, Message, ResponseFormat, StreamEvent, TextDelta, ToolCall, ToolDefinition,
    Usage,
)
from .base import AdapterError, model_id_for

NAME = "gemini"

#: Whether this wire format offers a native speech-to-speech session of its
#: own (`open_realtime_session`, below) — read by `voice/options.py` to
#: decide which providers can offer the "full duplex" engine. A fact about
#: the SDK, not something discovery could ever learn per-model, so it lives
#: here rather than on `ai/capabilities.py`.
SUPPORTS_REALTIME = True


class NoCredential(AdapterError):
    pass


def _client(provider: Any):
    from google import genai

    from ..credentials import resolve

    key = resolve(provider.credential_ref)
    if not key:
        raise NoCredential("No API key configured.")
    if provider.base_url:
        from google.genai import types as genai_types
        return genai.Client(api_key=key, http_options=genai_types.HttpOptions(base_url=provider.base_url))
    return genai.Client(api_key=key)


def _media_parts(attachments) -> list[dict[str, Any]]:
    parts = []
    for a in attachments or ():
        if a.data:
            import base64
            parts.append({"inlineData": {"mimeType": a.mime_type or "image/png",
                                         "data": base64.b64encode(a.data).decode("ascii")}})
        elif a.uri:
            parts.append({"fileData": {"mimeType": a.mime_type, "fileUri": a.uri}})
    return parts


def to_wire(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user" and (m.text or m.attachments):
            parts = ([{"text": m.text}] if m.text else []) + _media_parts(m.attachments)
            contents.append({"role": "user", "parts": parts})
        elif m.role == "assistant":
            raw = m.raw or {}
            if raw.get("adapter") == NAME and raw.get("content"):
                contents.append(raw["content"])
            elif m.tool_calls:
                contents.append({"role": "model", "parts": [
                    {"functionCall": {"name": c.name, "args": c.args}} for c in m.tool_calls]})
            elif m.text:
                contents.append({"role": "model", "parts": [{"text": m.text}]})
        elif m.role == "tool" and m.tool_call_id:
            contents.append({"role": "user", "parts": [
                {"functionResponse": {"name": m.tool_name or m.tool_call_id,
                                      "response": _response_of(m)}}]})
    return contents


def _response_of(message: Message) -> dict[str, Any]:
    """Gemini requires a functionResponse's `response` to be an object — a
    tool that returns a bare string or number is legal everywhere else, so
    it is wrapped rather than rejected. `message.text` is the tool's result,
    already serialized to JSON by whoever built this message; parsed back
    here because this wire wants an object, not a string."""
    import json

    try:
        parsed = json.loads(message.text) if message.text else None
    except ValueError:
        parsed = message.text
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _thinking_config(reasoning: ReasoningRequest | None) -> dict[str, Any] | None:
    if reasoning is None:
        return None
    if reasoning.kind is ReasoningKind.TIERS and reasoning.native:
        return {"thinkingLevel": reasoning.native}
    if reasoning.kind is ReasoningKind.BUDGET:
        budget = reasoning.native
        return {"thinkingBudget": int(budget)} if budget else None
    return None


def _generation_config(params: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "temperature" in params:
        out["temperature"] = params["temperature"]
    if "top_p" in params:
        out["topP"] = params["top_p"]
    if "top_k" in params:
        out["topK"] = params["top_k"]
    if "max_output_tokens" in params:
        out["maxOutputTokens"] = params["max_output_tokens"]
    if "stop_sequences" in params:
        out["stopSequences"] = list(params["stop_sequences"])
    return out


def _usage(metadata: Any) -> Usage | None:
    if metadata is None:
        return None
    tokens_in = getattr(metadata, "prompt_token_count", None)
    tokens_out = getattr(metadata, "candidates_token_count", None)
    cached_in = getattr(metadata, "cached_content_token_count", None)
    if tokens_in is None and tokens_out is None and cached_in is None:
        return None
    return Usage(tokens_in=tokens_in, tokens_out=tokens_out, cached_in=cached_in)


def stream(
    provider: Any,
    native_model_id: str,
    messages: tuple[Message, ...],
    system: str,
    tools: tuple[ToolDefinition, ...],
    *,
    reasoning: ReasoningRequest | None,
    params: Mapping[str, Any],
    response_format: ResponseFormat | None,
) -> Iterator[StreamEvent]:
    client = _client(provider)
    config: dict[str, Any] = dict(_generation_config(params))
    if system:
        config["systemInstruction"] = system
    if tools:
        config["tools"] = [{"functionDeclarations": [
            {"name": t.name, "description": t.description,
             "parameters": t.parameters or {"type": "object", "properties": {}}}
            for t in tools]}]
    thinking = _thinking_config(reasoning)
    if thinking:
        config["thinkingConfig"] = thinking
    if response_format is not None:
        config["responseMimeType"] = "application/json"
        config["responseSchema"] = response_format.schema

    turn_parts: list[Any] = []
    calls: list[Any] = []
    text = ""
    usage: Usage | None = None

    for chunk in client.models.generate_content_stream(
        model=model_id_for(native_model_id, reasoning), contents=to_wire(messages),
        config=config or None,
    ):
        usage = _usage(getattr(chunk, "usage_metadata", None)) or usage
        candidates = getattr(chunk, "candidates", None) or []
        if candidates and candidates[0].content and candidates[0].content.parts:
            turn_parts.extend(candidates[0].content.parts)
        if getattr(chunk, "text", None):
            text += chunk.text
            yield TextDelta(chunk.text)
        for call in getattr(chunk, "function_calls", None) or []:
            calls.append(call)

    raw = {"adapter": NAME, "content": {"role": "model", "parts": turn_parts}}

    if calls:
        yield Completed(
            text=text,
            tool_calls=tuple(
                ToolCall(getattr(c, "id", None) or f"call_{i}", c.name, dict(c.args or {}))
                for i, c in enumerate(calls)),
            finish_reason="tool_calls", usage=usage, raw=raw,
        )
        return

    if not text:
        raise AdapterError("The model returned an empty response — try again later.")
    if not turn_parts:
        raw["content"]["parts"] = [{"text": text}]
    yield Completed(text=text, finish_reason="stop", usage=usage, raw=raw)


def test_connection(provider: Any, native_model_id: str) -> dict[str, Any]:
    try:
        client = _client(provider)
        response = client.models.generate_content(
            model=native_model_id, contents='Say "ready" and nothing else.')
        if not getattr(response, "text", None):
            return {"ok": False, "error": "The server responded but with no text — check the model name."}
        return {"ok": True, "error": None}
    except NoCredential:
        return {"ok": False, "error": "That connection needs an API key."}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(err)}


def discover_models(provider: Any) -> list[dict[str, Any]]:
    client = _client(provider)
    out = []
    for model in client.models.list():
        model_id = (model.name or "").removeprefix("models/")
        if model_id:
            out.append({"model": model_id, "label": getattr(model, "display_name", None),
                        "contextTokens": getattr(model, "input_token_limit", None)})
    return out


def friendly_error(err: BaseException) -> str:
    """Gemini's `.message` is raw JSON — the readable half is nested inside."""
    import json

    try:
        parsed = json.loads(str(getattr(err, "message", "") or err))
        message = parsed.get("error", {}).get("message")
        if isinstance(message, str) and message:
            return message
    except Exception:  # noqa: BLE001
        pass
    return str(err) or "That connection didn't work."


# --- putting a real file where this provider can read it ---------------------
# The only adapter that implements this, which is a fact about the providers
# rather than a gap here: it is what makes watching a video or listening to a
# real audio file possible at all.

UPLOAD_TIMEOUT_S = 5 * 60
UPLOAD_POLL_S = 2.0


def upload_file(provider: Any, path: str, mime_type: str | None = None,
                *, timeout_s: float = UPLOAD_TIMEOUT_S) -> dict[str, Any]:
    """Upload a real file and wait until it can actually be read.

    Polls rather than returning immediately: a file still PROCESSING is
    accepted by the upload call and then rejected by the generate call,
    which surfaces as "that video is invalid" instead of "it wasn't ready
    yet". The returned URI belongs to THIS provider's key — a caller must
    not fall back to another model after using it.
    """
    import time

    client = _client(provider)
    config = {"mime_type": mime_type} if mime_type else None
    uploaded = client.files.upload(file=path, config=config)

    started = time.monotonic()
    while getattr(uploaded, "state", None) and str(uploaded.state).endswith("PROCESSING"):
        if time.monotonic() - started > timeout_s:
            raise AdapterError("That file took too long to process — it may be too large.")
        time.sleep(UPLOAD_POLL_S)
        uploaded = client.files.get(name=uploaded.name)

    if getattr(uploaded, "state", None) and str(uploaded.state).endswith("FAILED"):
        detail = getattr(getattr(uploaded, "error", None), "message", None)
        raise AdapterError(detail or "That file couldn't be processed.")

    def cleanup() -> None:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:  # noqa: BLE001 — it expires on its own within ~48h
            pass

    return {"uri": uploaded.uri, "mimeType": getattr(uploaded, "mime_type", None) or mime_type,
            "cleanup": cleanup}


# --- a realtime (speech-to-speech) session ------------------------------------
# Declared by SUPPORTS_REALTIME above. An adapter that flips that flag on is
# promising this function exists, which is what lets the voice routes ask for
# a session without knowing whose it is.

#: Used when the model isn't named explicitly for the session.
DEFAULT_REALTIME_MODEL = "gemini-live-2.5-flash-preview"


def open_realtime_session(provider: Any, native_model_id: str | None = None, *, config: dict[str, Any]):
    """An async context manager yielding a live speech-to-speech session.

    Kept here, not in a route, for the reason the architecture test states:
    a caller constructing a provider SDK directly is how a voice path ends up
    with none of the model system's protections.
    """
    from google import genai

    from ..credentials import resolve

    key = resolve(provider.credential_ref)
    if not key:
        raise NoCredential("No API key configured.")
    client = genai.Client(api_key=key)
    model = str(native_model_id or DEFAULT_REALTIME_MODEL)
    return client.aio.live.connect(model=model, config=config)
