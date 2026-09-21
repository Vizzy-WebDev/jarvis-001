"""A real, minimal model provider on a real socket — for testing `jarvis.models`
against an actual HTTP round trip and actual server-sent-event streams rather than
mocking the module under test.

One instance speaks ONE wire format (OpenAI Responses, OpenAI chat, Anthropic
Messages or Gemini generateContent), in that format's genuine shapes: the auth
header it really uses, its model-list response, and a streamed reply built from the
events that format really sends. It records every request, so a test can assert what
was actually put on the wire — the model id, the key, the effort — not what the code
under test believed it sent.

Scriptable through the constructor, each flag standing for something a real provider
does: a wrong key (401), a model it doesn't know (404), a list it won't give (404 or
403), a reply that just stops, a different model answering than the one asked for.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

FORMATS = ("openai-responses", "openai-chat", "anthropic-messages", "gemini-generatecontent")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that has read what it needs closes the socket while the server
        # is still writing the tail of the stream. That is ordinary, not a fault.
        pass


class StubProvider:
    def __init__(self, format: str, *, key: str | None = None,
                 models: list[dict[str, Any]] | None = None, list_status: int = 200,
                 reply: str = "Hello from the stub.", tool: tuple[str, dict[str, Any]] | None = None,
                 answers_as: str | None = None, unknown_model: str | None = None,
                 truncate: bool = False, chat_status: int | None = None) -> None:
        assert format in FORMATS
        self.format = format
        self.key = key
        self.models = models if models is not None else [{"id": "stub-model-a"}, {"id": "stub-model-b"}]
        self.list_status = list_status
        self.reply = reply
        self.tool = tool
        self.answers_as = answers_as
        self.unknown_model = unknown_model
        self.truncate = truncate
        self.chat_status = chat_status
        #: Every request received: {method, path, query, headers, body}.
        self.requests: list[dict[str, Any]] = []
        self.base_url = ""
        self._server: HTTPServer | None = None

    @property
    def prefix(self) -> str:
        """Where this format's API lives on the server."""
        return "/v1beta" if self.format == "gemini-generatecontent" else "/v1"

    # --- what a test reads back -----------------------------------------------------

    def posts(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]

    def last_body(self) -> dict[str, Any]:
        return self.posts()[-1]["body"]

    # --- lifecycle ------------------------------------------------------------------

    def start(self) -> str:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _send_json(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _received_key(self) -> str:
                bearer = self.headers.get("Authorization") or ""
                return (bearer.removeprefix("Bearer ") or self.headers.get("x-api-key")
                        or self.headers.get("x-goog-api-key") or "")

            def _authorised(self) -> bool:
                if stub.key is None:
                    return True
                if stub.format in ("openai-responses", "openai-chat"):
                    return self.headers.get("Authorization") == f"Bearer {stub.key}"
                if stub.format == "anthropic-messages":
                    return self.headers.get("x-api-key") == stub.key
                return self.headers.get("x-goog-api-key") == stub.key

            def _error(self, status: int, message: str) -> None:
                body: dict[str, Any] = {"error": {"message": message}}
                if stub.format == "gemini-generatecontent":
                    body = {"error": {"code": status, "message": message, "status": "ERROR"}}
                self._send_json(status, body)

            def _record(self, body: Any) -> None:
                parsed = urlparse(self.path)
                stub.requests.append({"method": self.command, "path": parsed.path,
                                      "query": parse_qs(parsed.query),
                                      "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "body": body})

            def do_GET(self) -> None:  # noqa: N802
                self._record(None)
                if not self._authorised():
                    # Real providers echo what they were sent. That is why errors are scrubbed.
                    return self._error(401, f"Incorrect API key provided: {self._received_key()}")
                path = urlparse(self.path).path
                # Only what a real provider serves: under its own prefix, and only the list.
                if not path.startswith(stub.prefix) or not path.endswith("/models"):
                    return self._error(404, "no such route")
                if stub.list_status != 200:
                    return self._error(stub.list_status, "the model list is not available here")
                self._send_json(200, stub._listing())

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self._record(body)
                if not self._authorised():
                    return self._error(401, f"Incorrect API key provided: {self._received_key()}")
                path = urlparse(self.path).path
                if not path.startswith(stub.prefix) or not stub._is_generation_path(path):
                    return self._error(404, "no such route")
                if not body:  # a probe: a real endpoint complains about the request
                    return self._error(400, "you must provide a model parameter")
                if stub.chat_status:
                    return self._error(stub.chat_status, "the provider is having a bad day")
                model = stub._requested_model(body, path)
                if stub.unknown_model and model == stub.unknown_model:
                    return self._error(404, f"The model `{model}` does not exist or you do not have access to it.")
                events = stub._events(body, model)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in events:
                    payload = chunk.encode()
                    self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self._server = _Server(("127.0.0.1", 0), Handler)
        # A short poll, so stopping it doesn't idle half a second per test.
        threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True).start()
        root = f"http://127.0.0.1:{self._server.server_port}"
        self.base_url = root + self.prefix
        return self.base_url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # --- the shapes each format really uses -----------------------------------------------

    def _listing(self) -> Any:
        if self.format == "anthropic-messages":
            return {"data": [{"type": "model", "id": m["id"], "display_name": m.get("display_name", m["id"]),
                              "max_tokens": m.get("max_tokens", 0), "capabilities": m.get("capabilities")}
                             for m in self.models], "has_more": False, "first_id": None, "last_id": None}
        if self.format == "gemini-generatecontent":
            return {"models": [{"name": f"models/{m['id']}", "displayName": m.get("display_name", m["id"]),
                                "supportedGenerationMethods": m.get("methods", ["generateContent"])}
                               for m in self.models]}
        #: What a gateway such as OmniRoute or OpenRouter adds beyond the id, passed through
        #: as given so a test can say what the provider "reported".
        extra = ("type", "capabilities", "architecture", "input_modalities", "output_modalities",
                 "supported_parameters", "pricing")
        return {"object": "list", "data": [{"id": m["id"], "object": "model", "created": m.get("created", 1),
                                            "owned_by": "stub", **{k: m[k] for k in extra if k in m}}
                                           for m in self.models]}

    def _is_generation_path(self, path: str) -> bool:
        return path.endswith({"openai-responses": "/responses", "openai-chat": "/chat/completions",
                              "anthropic-messages": "/messages"}.get(self.format, ":streamGenerateContent"))

    def _requested_model(self, body: dict[str, Any], path: str) -> str:
        if self.format == "gemini-generatecontent":
            return path.rsplit("/models/", 1)[-1].split(":", 1)[0]
        return body.get("model", "")

    @staticmethod
    def _sse(data: Any, event: str | None = None) -> str:
        head = f"event: {event}\n" if event else ""
        return f"{head}data: {data if isinstance(data, str) else json.dumps(data)}\n\n"

    def _has_tool_result(self, body: dict[str, Any]) -> bool:
        text = json.dumps(body)
        return any(m in text for m in ('"function_call_output"', '"tool_result"', '"functionResponse"', '"role": "tool"'))

    def _wants_tool(self, body: dict[str, Any]) -> bool:
        return bool(self.tool and body.get("tools") and not self._has_tool_result(body))

    def _events(self, body: dict[str, Any], model: str) -> list[str]:
        answered = self.answers_as or model
        return {"openai-responses": self._responses, "openai-chat": self._chat,
                "anthropic-messages": self._anthropic, "gemini-generatecontent": self._gemini}[self.format](
                    body, answered)

    def _words(self) -> list[str]:
        return [w + " " for w in self.reply.split(" ")][:-1] + [self.reply.split(" ")[-1]]

    def _responses(self, body: dict[str, Any], model: str) -> list[str]:
        out: list[str] = []
        usage = {"input_tokens": 12, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 4},
                 "output_tokens_details": {"reasoning_tokens": 2}}
        if self._wants_tool(body):
            name, args = self.tool  # type: ignore[misc]
            item = {"type": "function_call", "call_id": "call_stub1", "name": name, "arguments": json.dumps(args)}
            out.append(self._sse({"type": "response.function_call_arguments.delta", "delta": json.dumps(args)}))
            output = [item]
        else:
            for word in self._words():
                out.append(self._sse({"type": "response.output_text.delta", "delta": word}))
            output = [{"type": "message", "content": [{"type": "output_text", "text": self.reply}]}]
        if not self.truncate:
            out.append(self._sse({"type": "response.completed", "response": {
                "id": "resp_1", "model": model, "status": "completed", "output": output, "usage": usage}}))
        return out

    def _chat(self, body: dict[str, Any], model: str) -> list[str]:
        out: list[str] = []
        if self._wants_tool(body):
            name, args = self.tool  # type: ignore[misc]
            raw = json.dumps(args)
            out.append(self._sse({"model": model, "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_stub1", "type": "function", "function": {"name": name, "arguments": ""}}]}}]}))
            for i in range(0, len(raw), 6):
                out.append(self._sse({"model": model, "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "function": {"arguments": raw[i:i + 6]}}]}}]}))
            finish = "tool_calls"
        else:
            for word in self._words():
                out.append(self._sse({"model": model, "choices": [{"index": 0, "delta": {"content": word}}]}))
            finish = "stop"
        if not self.truncate:
            out.append(self._sse({"model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
            out.append(self._sse({"model": model, "choices": [], "usage": {
                "prompt_tokens": 12, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 4}}}))
            out.append("data: [DONE]\n\n")
        return out

    def _anthropic(self, body: dict[str, Any], model: str) -> list[str]:
        e = self._sse
        out = [e({"type": "message_start", "message": {"id": "msg_1", "model": model, "usage": {
            "input_tokens": 12, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 0,
            "output_tokens": 1}}}, "message_start"),
               # Thinking is on by default on the newest models: text omitted, signature present.
               e({"type": "content_block_start", "index": 0,
                  "content_block": {"type": "thinking", "thinking": "", "signature": ""}}, "content_block_start"),
               e({"type": "content_block_delta", "index": 0,
                  "delta": {"type": "thinking_delta", "thinking": ""}}, "content_block_delta"),
               e({"type": "content_block_delta", "index": 0,
                  "delta": {"type": "signature_delta", "signature": "SIG-abc123"}}, "content_block_delta"),
               e({"type": "content_block_stop", "index": 0}, "content_block_stop")]
        if self._wants_tool(body):
            name, args = self.tool  # type: ignore[misc]
            raw = json.dumps(args)
            out.append(e({"type": "content_block_start", "index": 1, "content_block": {
                "type": "tool_use", "id": "toolu_stub1", "name": name, "input": {}}}, "content_block_start"))
            for i in range(0, len(raw), 5):
                out.append(e({"type": "content_block_delta", "index": 1,
                              "delta": {"type": "input_json_delta", "partial_json": raw[i:i + 5]}},
                             "content_block_delta"))
            out.append(e({"type": "content_block_stop", "index": 1}, "content_block_stop"))
            stop = "tool_use"
        else:
            out.append(e({"type": "content_block_start", "index": 1,
                          "content_block": {"type": "text", "text": ""}}, "content_block_start"))
            for word in self._words():
                out.append(e({"type": "content_block_delta", "index": 1,
                              "delta": {"type": "text_delta", "text": word}}, "content_block_delta"))
            out.append(e({"type": "content_block_stop", "index": 1}, "content_block_stop"))
            stop = "end_turn"
        if not self.truncate:
            out.append(e({"type": "message_delta", "delta": {"stop_reason": stop},
                          "usage": {"output_tokens": 7}}, "message_delta"))
            out.append(e({"type": "message_stop"}, "message_stop"))
        return out

    def _gemini(self, body: dict[str, Any], model: str) -> list[str]:
        out: list[str] = []
        if self._wants_tool(body):
            name, args = self.tool  # type: ignore[misc]
            out.append(self._sse({"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": name, "args": args}, "thoughtSignature": "GEMSIG-xyz"}]},
                "finishReason": "STOP"}], "modelVersion": model,
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 7, "cachedContentTokenCount": 4,
                                  "thoughtsTokenCount": 2}}))
        else:
            words = self._words()
            for i, word in enumerate(words):
                last = i == len(words) - 1
                chunk: dict[str, Any] = {"candidates": [{"content": {"role": "model", "parts": [{"text": word}]}}],
                                         "modelVersion": model}
                if last and not self.truncate:
                    chunk["candidates"][0]["finishReason"] = "STOP"
                    chunk["usageMetadata"] = {"promptTokenCount": 12, "candidatesTokenCount": 7,
                                              "cachedContentTokenCount": 4, "thoughtsTokenCount": 2}
                out.append(self._sse(chunk))
        return out
