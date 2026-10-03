"""A real HTTP API for connector tests, on a real socket.

It behaves the ways real key-based services were found to behave, so a test
against it exercises what the real ones need:
  - a key in a header (`Authorization: Key <key>`), refused with 401 otherwise;
  - a "does this request exist" endpoint that answers 404 for a good key and 401
    for a bad one (how the Higgsfield API was found to answer);
  - an endpoint that ALWAYS answers HTTP 200 and puts the verdict in the body
    (`{"code": 401, "msg": ...}` — how Kie.ai was found to answer);
  - a background job: create returns a task id, then status goes waiting ->
    generating -> success (or -> fail for the "broken" model);
  - its own OpenAPI document, in YAML.
Every request is recorded with the headers it came with.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

GOOD_KEY = "kid:ksecret"

OPENAPI_YAML = """openapi: 3.0.0
info: {title: Stub Pets, version: "1"}
servers: [{url: /v1}]
components:
  securitySchemes:
    petKey: {type: apiKey, in: header, name: X-Pet-Key}
  schemas:
    Pet:
      type: object
      required: [name]
      properties:
        name: {type: string, description: The pet's name.}
        tags: {type: array}
paths:
  /pets/{petId}:
    get:
      operationId: getPet
      summary: Find a pet by its id.
      parameters:
        - {name: petId, in: path, required: true, schema: {type: integer}}
        - {name: verbose, in: query, schema: {type: boolean}}
  /pets:
    post:
      operationId: addPet
      summary: Add a pet.
      requestBody:
        required: true
        content:
          application/json:
            schema: {$ref: '#/components/schemas/Pet'}
"""


class StubApi:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.url = ""
        self._server: ThreadingHTTPServer | None = None

    def __enter__(self) -> "StubApi":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def _send(self, status: int, payload: Any, content_type: str = "application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                auth = self.headers.get("Authorization")
                stub.calls.append({"method": method, "path": parsed.path, "query": query,
                                   "body": body, "auth": auth,
                                   "petKey": self.headers.get("X-Pet-Key")})
                good = auth == f"Key {GOOD_KEY}"
                path = parsed.path

                if path == "/openapi.yaml":
                    return self._send(200, OPENAPI_YAML.encode(), "application/yaml")
                if path.startswith("/requests/") and path.endswith("/status"):
                    return self._send(404 if good else 401,
                                      {"detail": "Not found" if good else "Invalid credentials"})
                if path == "/credit":  # always HTTP 200; the verdict is in the body
                    return self._send(200, {"code": 200, "msg": "success", "data": 42} if good
                                      else {"code": 401, "msg": "Unauthorized - check your key"})
                if not good and not path.startswith("/v1/"):
                    return self._send(401, {"detail": "Invalid credentials"})
                if path == "/jobs/create":
                    task_id = f"t{len(stub.jobs) + 1}"
                    model = (body or {}).get("model")
                    stub.jobs[task_id] = {"polls": 0, "model": model}
                    return self._send(200, {"code": 200, "data": {"taskId": task_id}})
                if path == "/jobs/status":
                    job = stub.jobs.get(query.get("taskId") or "")
                    if job is None:
                        return self._send(200, {"code": 422, "msg": "no such task"})
                    job["polls"] += 1
                    if job["model"] == "slow":
                        state = "generating"
                    elif job["polls"] < 3:
                        state = ["waiting", "generating"][job["polls"] - 1]
                    else:
                        state = "fail" if job["model"] == "broken" else "success"
                    result = {"resultUrls": ["https://files.example/result.png"]} \
                        if state == "success" else None
                    return self._send(200, {"code": 200, "data": {"taskId": query["taskId"],
                                                                  "state": state,
                                                                  "result": result}})
                if path.startswith("/v1/pets"):
                    if self.headers.get("X-Pet-Key") != "pet-key":
                        return self._send(401, {"detail": "no pet key"})
                    if method == "GET":
                        return self._send(200, {"id": path.rsplit("/", 1)[-1], "name": "Rex",
                                                "verbose": query.get("verbose")})
                    return self._send(201, {"created": body})
                return self._send(404, {"detail": "no such route"})

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
