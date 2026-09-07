"""Gemini — the generateContent wire format.

Two things must be right here or the next call is rejected outright:

* **`thought_signature` must round-trip verbatim.** The model's own
  `candidates[0].content` is replayed unchanged, never a hand-rebuilt
  `{role, parts}` object. This is why the neutral message carries `raw` at all.

* **Streamed chunks are incremental deltas, not cumulative.** Parts are
  concatenated across every chunk to reconstruct the turn.

The interrupt rule is narrower here than for the other adapters, deliberately: an
interrupted assistant turn normally replays only what was heard, but if it
carried tool calls the raw content is replayed IN FULL anyway, because dropping
it breaks the tool-calling protocol (a 400, not a cosmetic issue). Protecting the
protocol wins over exactness of replay in that one overlap.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..config import get_secret
from ..conversation import assistant_text_of
from ..orchestrator.model_port import ModelEvent, StepComplete, TextChunk, ToolCall
from . import usage as usage_read
from .base import AdapterError

name = "gemini"

#: Gemini is the only adapter here that implements video, audio and web search.
#: Seeds a model's caps; does not gate what a model may be asked to do.
CAPABILITIES = {"video": True, "audio": True, "vision": True, "webSearch": True}


class NoApiKey(AdapterError):
    pass


def _key(entry: dict[str, Any]) -> str | None:
    if "secretValue" in entry:
        return entry["secretValue"]
    ref = entry.get("secretRef")
    return get_secret(ref) if ref else None


def _client(entry: dict[str, Any]):
    from google import genai

    key = _key(entry)
    if not key:
        raise NoApiKey("No API key configured.")
    base = entry.get("baseUrl")
    if base:
        from google.genai import types as genai_types
        return genai.Client(api_key=key, http_options=genai_types.HttpOptions(base_url=base))
    return genai.Client(api_key=key)


def _media_parts(media: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    parts = []
    for item in media or []:
        if item.get("dataBase64"):
            parts.append({"inlineData": {"mimeType": item.get("mimeType", "image/png"),
                                         "data": item["dataBase64"]}})
        elif item.get("uri"):
            parts.append({"fileData": {"mimeType": item.get("mimeType"), "fileUri": item["uri"]}})
    return parts


def to_wire(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "user" and (m.get("text") or m.get("media")):
            parts = ([{"text": m["text"]}] if m.get("text") else []) + _media_parts(m.get("media"))
            contents.append({"role": "user", "parts": parts})
        elif role == "assistant":
            spoken = assistant_text_of(m)
            raw = m.get("raw") or {}
            replayable = raw.get("adapter") == "gemini" and raw.get("content")
            if replayable and (m.get("toolCalls") or not m.get("interrupted")):
                contents.append(raw["content"])
            elif m.get("toolCalls"):
                contents.append({"role": "model", "parts": [
                    {"functionCall": {"name": c.get("name"), "args": c.get("args") or {}}}
                    for c in m["toolCalls"]]})
            elif spoken:
                contents.append({"role": "model", "parts": [{"text": spoken}]})
        elif role == "tool" and m.get("toolResults"):
            contents.append({"role": "user", "parts": [
                {"functionResponse": {"name": r.get("name"), "response": _response_of(r)}}
                for r in m["toolResults"]]})
    return contents


def _response_of(result_row: dict[str, Any]) -> dict[str, Any]:
    """Gemini requires a functionResponse's `response` to be an object.

    A tool that returns a bare string or number is perfectly legal everywhere
    else, so it is wrapped rather than rejected.
    """
    value = result_row.get("result")
    return value if isinstance(value, dict) else {"result": value}


def stream(
    entry: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
) -> Iterator[ModelEvent]:
    client = _client(entry)
    config: dict[str, Any] = {"systemInstruction": system} if system else {}
    if tools:
        config["tools"] = [{"functionDeclarations": tools}]

    turn_parts: list[Any] = []
    calls: list[Any] = []
    text = ""
    usage: dict[str, int] | None = None

    for chunk in client.models.generate_content_stream(
        model=entry["model"], contents=to_wire(messages), config=config or None
    ):
        # Gemini reports the turn's CUMULATIVE totals on each chunk, so the
        # last one seen is the whole turn — summing would multiply one turn's
        # real cost by however many chunks it arrived in.
        usage = usage_read.from_gemini(getattr(chunk, "usage_metadata", None)) or usage

        candidates = getattr(chunk, "candidates", None) or []
        if candidates and candidates[0].content and candidates[0].content.parts:
            turn_parts.extend(candidates[0].content.parts)
        if getattr(chunk, "text", None):
            text += chunk.text
            yield TextChunk(chunk.text)
        for call in getattr(chunk, "function_calls", None) or []:
            calls.append(call)

    raw = {"adapter": "gemini", "content": {"role": "model", "parts": turn_parts}}

    if calls:
        yield StepComplete(
            text=text,
            tool_calls=tuple(
                ToolCall(getattr(c, "id", None) or f"call_{i}", c.name, dict(c.args or {}))
                for i, c in enumerate(calls)),
            model_id=entry.get("id"), raw=raw, usage=usage,
        )
        return

    if not text:
        raise AdapterError("The model returned an empty response — try again later.")

    if not turn_parts:
        raw["content"]["parts"] = [{"text": text}]
    yield StepComplete(text=text, model_id=entry.get("id"), raw=raw, usage=usage)


UPLOAD_TIMEOUT_S = 5 * 60
UPLOAD_POLL_S = 2.0


def upload_file(entry: dict[str, Any], path: str, mime_type: str | None = None,
                *, timeout_s: float = UPLOAD_TIMEOUT_S) -> dict[str, Any]:
    """Put a real file where this provider can read it, and wait until it can.

    The only adapter that implements this, which is a fact about the providers
    rather than a gap here: it is what makes watching a video or listening to a
    real audio file possible at all. Callers must not fall back to another model
    after using it — the returned URI belongs to THIS key.

    Polls rather than returning immediately: a file still PROCESSING is accepted
    by the upload call and then rejected by the generate call, which surfaces as
    "that video is invalid" instead of "it wasn't ready yet".
    """
    import time

    client = _client(entry)
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


def test_connection(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        client = _client(entry)
        response = client.models.generate_content(
            model=entry["model"], contents='Say "ready" and nothing else.')
        if not getattr(response, "text", None):
            return {"ok": False, "error": "The server responded but with no text — check the model name."}
        return {"ok": True}
    except NoApiKey:
        return {"ok": False, "error": "That connection needs an API key.", "friendly": True}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(err)}


def list_models(entry: dict[str, Any]) -> list[dict[str, Any]]:
    client = _client(entry)
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
    except Exception:
        pass
    return str(err) or "That connection didn't work."
