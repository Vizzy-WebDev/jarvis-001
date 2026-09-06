"""A stub OpenAI-shaped server, for exercising the real adapter over real HTTP.

The project's own established technique: when quota is gone (which, on this
roster, is the normal state rather than an edge case), a small canned server is
how a pipeline gets verified end to end. This one speaks enough of the wire
format for the adapter under test to be the REAL adapter, not a mock — including
the SSE framing, the empty-`choices` usage chunk, and tool-call deltas.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubModelServer:
    """Scripted responses, one per request, plus a request log."""

    def __init__(self) -> None:
        self.script: list[dict] = []
        self.requests: list[dict] = []
        self.models = [{"id": "stub-model", "object": "model"}]
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # silence the default stderr spam
                pass

            def _send(self, status: int, body: bytes, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.endswith("/models"):
                    step = outer._next(listing=True)
                    if step.get("status", 200) != 200:
                        self._send(step["status"], json.dumps(
                            {"error": {"message": step.get("message", "no")}}).encode())
                        return
                    self._send(200, json.dumps({"object": "list", "data": outer.models}).encode())
                    return
                self._send(404, b'{"error":{"message":"not found"}}')

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path, "body": payload,
                                       "auth": self.headers.get("Authorization")})
                step = outer._next()

                status = step.get("status", 200)
                if status != 200:
                    self._send(status, json.dumps(
                        {"error": {"message": step.get("message", "upstream said no")}}).encode())
                    return

                if payload.get("stream"):
                    self._stream(step)
                else:
                    self._send(200, json.dumps(_completion(step)).encode())

            def _stream(self, step: dict):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for frame in _sse_frames(step):
                    self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # --- scripting -----------------------------------------------------------

    def says(self, text: str, **kw):
        self.script.append({"text": text, **kw})
        return self

    def calls_tool(self, name: str, args: dict, call_id: str = "call_1"):
        self.script.append({"tool": {"id": call_id, "name": name, "args": args}})
        return self

    def fails(self, status: int, message: str = "no"):
        self.script.append({"status": status, "message": message})
        return self

    def _next(self, listing: bool = False) -> dict:
        # A listing request consumes a scripted failure (so "this key can list
        # but not generate" is expressible) but never a scripted reply.
        if listing:
            if self.script and "status" in self.script[0]:
                return self.script.pop(0)
            return {}
        return self.script.pop(0) if self.script else {"text": "ok"}


def _completion(step: dict) -> dict:
    return {"id": "c", "object": "chat.completion", "model": "stub-model",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": step.get("text", "ready")},
                         "finish_reason": "stop"}]}


def _sse_frames(step: dict) -> list[dict]:
    frames: list[dict] = []
    tool = step.get("tool")
    if tool:
        frames.append(_frame({"tool_calls": [
            {"index": 0, "id": tool["id"], "type": "function",
             "function": {"name": tool["name"], "arguments": json.dumps(tool["args"])}}]}))
    else:
        for piece in _split(step.get("text", "ok")):
            frames.append(_frame({"content": piece}))
    # The usage-only final chunk: a real one carries an EMPTY choices array,
    # which is exactly the shape that used to be skipped unread.
    frames.append({"id": "c", "object": "chat.completion.chunk", "choices": [],
                   "usage": {"prompt_tokens": 11, "completion_tokens": 3}})
    return frames


def _frame(delta: dict) -> dict:
    return {"id": "c", "object": "chat.completion.chunk", "model": "stub-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def _split(text: str) -> list[str]:
    """Deliberately awkward chunking — a real provider does not send whole
    sentences, and anything that only works on whole chunks is broken."""
    return [text[i:i + 3] for i in range(0, len(text), 3)] or [""]
