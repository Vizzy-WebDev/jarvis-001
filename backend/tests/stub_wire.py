"""A real model server on a real socket, speaking one of the four wire formats in
its genuine shapes — for the driver conformance suite.

Each generation request plays the next scripted `Turn` (or a plain answer when
none is queued): text, several tool calls at once, tool calls with or without the
server's own ids, provider reasoning state (a signed thinking block, encrypted
reasoning, a thought signature), a refusal with any HTTP status, an error inside a
200, an error part way through the stream, a reply that just stops. It records
every request, so a test asserts what was actually put on the wire.

Also served: the model list in each format's shape, Ollama's native `/api/show`,
and `/v1/embeddings`.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

FORMATS = ("openai_chat", "openai_responses", "anthropic_messages", "gemini_generate")


@dataclass
class Turn:
    text: str = "Hello from the stub."
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    ids: bool = True
    reasoning: bool = True
    status: int | None = None
    message: str = "the stub refused"
    retry_after: str | None = None
    #: An error frame part way through the stream, after the first piece of text.
    mid_stream: str | None = None
    #: The reply is a 200 whose content is the server's error: {"code": int, "message": str}.
    error_in_200: dict[str, Any] | None = None
    truncate: bool = False
    model: str | None = None
    finish: str | None = None  # None | "length" | "content_filter"
    usage: dict[str, int] = field(default_factory=lambda: {"input": 12, "output": 7, "cached": 4, "reasoning": 2})


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client closing early is ordinary


class StubWire:
    def __init__(self, fmt: str, *, key: str | None = None, models: list[dict[str, Any]] | None = None,
                 show: dict[str, dict[str, Any]] | None = None, list_status: int = 200) -> None:
        assert fmt in FORMATS
        self.fmt = fmt
        self.key = key
        self.models = models if models is not None else [{"id": "stub-a"}, {"id": "stub-b"}]
        self.show = show or {}
        self.list_status = list_status
        self.script: list[Turn] = []
        self.requests: list[dict[str, Any]] = []
        self.base_url = ""
        self._server: _Server | None = None
        self._lock = threading.Lock()

    # --- scripting and reading back ---------------------------------------------------------

    def queue(self, *turns: Turn) -> "StubWire":
        with self._lock:
            self.script.extend(turns)
        return self

    def generations(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST" and r["kind"] == "generate"]

    def last_body(self) -> dict[str, Any]:
        return self.generations()[-1]["body"]

    @property
    def prefix(self) -> str:
        return "/v1beta" if self.fmt == "gemini_generate" else "/v1"

    # --- lifecycle ------------------------------------------------------------------------------

    def start(self) -> str:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _authorised(self) -> bool:
                if stub.key is None:
                    return True
                h = self.headers
                return stub.key in ((h.get("Authorization") or "").removeprefix("Bearer "),
                                    h.get("x-api-key"), h.get("x-goog-api-key"))

            def _refuse(self, status: int, message: str, retry_after: str | None = None) -> None:
                body: dict[str, Any] = {"error": {"message": message, "code": status}}
                if stub.fmt == "anthropic_messages":
                    body = {"type": "error", "error": {"type": "api_error", "message": message}}
                self._json(status, body, {"Retry-After": retry_after} if retry_after else None)

            def _record(self, kind: str, body: Any) -> None:
                parsed = urlparse(self.path)
                with stub._lock:
                    stub.requests.append({"method": self.command, "path": parsed.path, "kind": kind,
                                          "query": parse_qs(parsed.query),
                                          "headers": {k.lower(): v for k, v in self.headers.items()},
                                          "body": body})

            def do_GET(self) -> None:  # noqa: N802
                self._record("list", None)
                if not self._authorised():
                    return self._refuse(401, "Incorrect API key provided")
                path = urlparse(self.path).path
                if not path.startswith(stub.prefix) or not path.endswith("/models"):
                    return self._refuse(404, "no such route")
                if stub.list_status != 200:
                    return self._refuse(stub.list_status, "the model list is not available here")
                self._json(200, stub._listing())

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/show":
                    self._record("show", body)
                    shown = stub.show.get(body.get("model"))
                    return self._json(200, shown) if shown else self._refuse(404, "model not found")
                if path.endswith("/embeddings"):
                    self._record("embed", body)
                    if not self._authorised():
                        return self._refuse(401, "Incorrect API key provided")
                    inputs = body.get("input") or []
                    return self._json(200, {"data": [{"index": i, "embedding": [float(len(t)), float(i), 0.5]}
                                                      for i, t in reversed(list(enumerate(inputs)))],
                                            "model": body.get("model")})
                self._record("generate", body)
                if not self._authorised():
                    return self._refuse(401, "Incorrect API key provided")
                if not path.startswith(stub.prefix) or not stub._is_generation(path):
                    return self._refuse(404, "no such route")
                with stub._lock:
                    turn = stub.script.pop(0) if stub.script else Turn()
                if turn.status:
                    return self._refuse(turn.status, turn.message, turn.retry_after)
                model = path.rsplit("/models/", 1)[-1].split(":", 1)[0] if stub.fmt == "gemini_generate" \
                    else body.get("model", "")
                frames = getattr(stub, f"_{stub.fmt}")(body, turn, turn.model or model)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for frame in frames:
                    payload = frame.encode()
                    self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self._server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True).start()
        self.root = f"http://127.0.0.1:{self._server.server_port}"
        self.base_url = self.root + self.prefix
        return self.base_url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # --- shapes -----------------------------------------------------------------------------------

    def _is_generation(self, path: str) -> bool:
        return path.endswith({"openai_chat": "/chat/completions", "openai_responses": "/responses",
                              "anthropic_messages": "/messages"}.get(self.fmt, ":streamGenerateContent"))

    def _listing(self) -> Any:
        if self.fmt == "anthropic_messages":
            return {"data": [{"type": "model", "id": m["id"], "display_name": m.get("display_name", m["id"]),
                              "max_tokens": m.get("max_tokens", 0), "max_input_tokens": m.get("max_input_tokens", 0),
                              "capabilities": m.get("capabilities")} for m in self.models],
                    "has_more": False, "first_id": None, "last_id": None}
        if self.fmt == "gemini_generate":
            return {"models": [{"name": f"models/{m['id']}", "displayName": m.get("display_name", m["id"]),
                                "inputTokenLimit": m.get("input", 0), "outputTokenLimit": m.get("output", 0),
                                "supportedGenerationMethods": m.get("methods", ["generateContent"]),
                                **({"thinking": m["thinking"]} if "thinking" in m else {})}
                               for m in self.models]}
        return {"object": "list", "data": [{"object": "model", "created": 1, "owned_by": "stub", **m}
                                           for m in self.models]}

    @staticmethod
    def _sse(data: Any, event: str | None = None) -> str:
        head = f"event: {event}\n" if event else ""
        return f"{head}data: {data if isinstance(data, str) else json.dumps(data)}\n\n"

    @staticmethod
    def _words(text: str) -> list[str]:
        words = text.split(" ")
        return [w + " " for w in words[:-1]] + [words[-1]] if text else []

    # openai chat ----------------------------------------------------------------------------------

    def _openai_chat(self, body: dict[str, Any], turn: Turn, model: str) -> list[str]:
        e = self._sse
        out: list[str] = []
        if turn.error_in_200:
            envelope = json.dumps({"error": turn.error_in_200})
            for i in range(0, len(envelope), 9):
                out.append(e({"model": model, "choices": [{"index": 0, "delta": {"content": envelope[i:i + 9]}}]}))
            out.append(e({"model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
            return out + ["data: [DONE]\n\n"]
        words = self._words(turn.text) if not turn.tools else []
        for i, word in enumerate(words):
            out.append(e({"model": model, "choices": [{"index": 0, "delta": {"content": word}}]}))
            if i == 0 and turn.mid_stream:
                out.append(e({"error": {"message": turn.mid_stream, "code": 503}}))
                return out
        for index, (name, args) in enumerate(turn.tools):
            first: dict[str, Any] = {"index": index, "type": "function", "function": {"name": name, "arguments": ""}}
            if turn.ids:
                first["id"] = f"chatcall_{index}"
            out.append(e({"model": model, "choices": [{"index": 0, "delta": {"tool_calls": [first]}}]}))
            raw = json.dumps(args)
            for j in range(0, len(raw), 5):
                out.append(e({"model": model, "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": index, "function": {"arguments": raw[j:j + 5]}}]}}]}))
        if turn.truncate:
            return out
        finish = {"length": "length", "content_filter": "content_filter"}.get(turn.finish or "",
                                                                             "tool_calls" if turn.tools else "stop")
        out.append(e({"model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
        u = turn.usage
        out.append(e({"model": model, "choices": [], "usage": {
            "prompt_tokens": u["input"], "completion_tokens": u["output"],
            "prompt_tokens_details": {"cached_tokens": u["cached"]},
            "completion_tokens_details": {"reasoning_tokens": u["reasoning"]}}}))
        return out + ["data: [DONE]\n\n"]

    # openai responses -----------------------------------------------------------------------------

    def _openai_responses(self, body: dict[str, Any], turn: Turn, model: str) -> list[str]:
        e = self._sse
        out = [e({"type": "response.created", "response": {"id": "resp_1", "model": model}})]
        output: list[dict[str, Any]] = []
        u = turn.usage
        usage = {"input_tokens": u["input"], "output_tokens": u["output"],
                 "input_tokens_details": {"cached_tokens": u["cached"]},
                 "output_tokens_details": {"reasoning_tokens": u["reasoning"]}}
        if turn.error_in_200:
            out.append(e({"type": "response.failed", "response": {"status": "failed", "error": {
                "code": str(turn.error_in_200.get("code")), "message": turn.error_in_200["message"]}}}))
            return out
        if turn.reasoning:
            item = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC-rs_1"}
            out.append(e({"type": "response.output_item.added", "output_index": 0,
                          "item": {"type": "reasoning", "id": "rs_1", "summary": []}}))
            out.append(e({"type": "response.output_item.done", "output_index": 0, "item": item}))
            output.append(item)
        if turn.text and not turn.tools:
            msg = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": turn.text, "annotations": []}]}
            out.append(e({"type": "response.output_item.added", "output_index": len(output),
                          "item": {**msg, "content": []}}))
            for i, word in enumerate(self._words(turn.text)):
                out.append(e({"type": "response.output_text.delta", "item_id": "msg_1", "delta": word}))
                if i == 0 and turn.mid_stream:
                    out.append(e({"type": "error", "code": "server_error", "message": turn.mid_stream}))
                    return out
            out.append(e({"type": "response.output_item.done", "output_index": len(output), "item": msg}))
            output.append(msg)
        for index, (name, args) in enumerate(turn.tools):
            raw = json.dumps(args)
            fc = {"type": "function_call", "id": f"fc_{index}", "call_id": f"respcall_{index}", "name": name,
                  "arguments": ""}
            out.append(e({"type": "response.output_item.added", "output_index": len(output), "item": fc}))
            for j in range(0, len(raw), 5):
                out.append(e({"type": "response.function_call_arguments.delta", "item_id": f"fc_{index}",
                              "output_index": len(output), "delta": raw[j:j + 5]}))
            done = {**fc, "arguments": raw, "status": "completed"}
            out.append(e({"type": "response.output_item.done", "output_index": len(output), "item": done}))
            output.append(done)
        if turn.truncate:
            return out
        if turn.finish == "length":
            out.append(e({"type": "response.incomplete", "response": {
                "id": "resp_1", "model": model, "status": "incomplete", "output": output, "usage": usage,
                "incomplete_details": {"reason": "max_output_tokens"}}}))
            return out
        out.append(e({"type": "response.completed", "response": {
            "id": "resp_1", "model": model, "status": "completed", "output": output, "usage": usage}}))
        return out

    # anthropic ------------------------------------------------------------------------------------

    def _anthropic_messages(self, body: dict[str, Any], turn: Turn, model: str) -> list[str]:
        e = self._sse
        u = turn.usage
        out = [e({"type": "message_start", "message": {"id": "msg_1", "model": model, "usage": {
            "input_tokens": u["input"] - u["cached"], "cache_read_input_tokens": u["cached"],
            "cache_creation_input_tokens": 0, "output_tokens": 1}}}, "message_start")]
        if turn.error_in_200:
            kind = {429: "rate_limit_error", 401: "authentication_error", 400: "invalid_request_error"}.get(
                turn.error_in_200.get("code"), "overloaded_error")
            out.append(e({"type": "error", "error": {"type": kind, "message": turn.error_in_200["message"]}},
                         "error"))
            return out
        index = 0
        if turn.reasoning:
            out += [e({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "thinking", "thinking": "", "signature": ""}}, "content_block_start"),
                    e({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "thinking_delta", "thinking": "Let me think."}}, "content_block_delta"),
                    e({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "signature_delta", "signature": "SIG-abc123"}}, "content_block_delta"),
                    e({"type": "content_block_stop", "index": 0}, "content_block_stop")]
            index = 1
        if turn.text and not turn.tools:
            out.append(e({"type": "content_block_start", "index": index,
                          "content_block": {"type": "text", "text": ""}}, "content_block_start"))
            for i, word in enumerate(self._words(turn.text)):
                out.append(e({"type": "content_block_delta", "index": index,
                              "delta": {"type": "text_delta", "text": word}}, "content_block_delta"))
                if i == 0 and turn.mid_stream:
                    out.append(e({"type": "error", "error": {"type": "overloaded_error",
                                                             "message": turn.mid_stream}}, "error"))
                    return out
            out.append(e({"type": "content_block_stop", "index": index}, "content_block_stop"))
            index += 1
        for n, (name, args) in enumerate(turn.tools):
            raw = json.dumps(args)
            out.append(e({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": f"toolu_{n}", "name": name, "input": {}}}, "content_block_start"))
            for j in range(0, len(raw), 5):
                out.append(e({"type": "content_block_delta", "index": index,
                              "delta": {"type": "input_json_delta", "partial_json": raw[j:j + 5]}},
                             "content_block_delta"))
            out.append(e({"type": "content_block_stop", "index": index}, "content_block_stop"))
            index += 1
        if turn.truncate:
            return out
        stop = {"length": "max_tokens", "content_filter": "refusal"}.get(turn.finish or "",
                                                                        "tool_use" if turn.tools else "end_turn")
        out.append(e({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": u["output"]}},
                     "message_delta"))
        out.append(e({"type": "message_stop"}, "message_stop"))
        return out

    # gemini ---------------------------------------------------------------------------------------

    def _gemini_generate(self, body: dict[str, Any], turn: Turn, model: str) -> list[str]:
        e = self._sse
        u = turn.usage
        usage = {"promptTokenCount": u["input"], "candidatesTokenCount": u["output"],
                 "cachedContentTokenCount": u["cached"], "thoughtsTokenCount": u["reasoning"]}
        if turn.error_in_200:
            return [e({"error": {"code": turn.error_in_200["code"], "message": turn.error_in_200["message"],
                                 "status": "ERROR"}})]
        finish = {"length": "MAX_TOKENS", "content_filter": "SAFETY"}.get(turn.finish or "", "STOP")
        out: list[str] = []
        if turn.tools:
            parts = []
            for n, (name, args) in enumerate(turn.tools):
                call: dict[str, Any] = {"name": name, "args": args}
                if turn.ids:
                    call["id"] = f"gemcall_{n}"
                part: dict[str, Any] = {"functionCall": call}
                if turn.reasoning and n == 0:
                    part["thoughtSignature"] = "GEMSIG-xyz"
                parts.append(part)
            chunk: dict[str, Any] = {"candidates": [{"content": {"role": "model", "parts": parts}}],
                                     "modelVersion": model}
            if not turn.truncate:
                chunk["candidates"][0]["finishReason"] = finish
                chunk["usageMetadata"] = usage
            return [e(chunk)]
        words = self._words(turn.text)
        for i, word in enumerate(words):
            part = {"text": word}
            if turn.reasoning and i == 0:
                part["thoughtSignature"] = "GEMSIG-text"
            chunk = {"candidates": [{"content": {"role": "model", "parts": [part]}}], "modelVersion": model}
            if i == len(words) - 1 and not turn.truncate:
                chunk["candidates"][0]["finishReason"] = finish
                chunk["usageMetadata"] = usage
            out.append(e(chunk))
            if i == 0 and turn.mid_stream:
                out.append(e({"error": {"code": 503, "message": turn.mid_stream, "status": "UNAVAILABLE"}}))
                return out
        return out
