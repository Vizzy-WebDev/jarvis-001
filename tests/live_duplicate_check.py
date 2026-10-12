"""Manual check: with SEVERAL working apps that can all make the same thing, does the person's own
model do the job ONCE?

    python tests/live_duplicate_check.py [--yes]

The apps are real MCP servers on this machine that demand their own token and COUNT every call
they receive, so "how many times was this image made" is read from the servers, not from what
the model says. Everything runs in a throw-away folder like `jarvis.routing_check`: only the
model settings and keys are copied in, no connector sign-in of the person's is.

Not part of the test suite (it spends a few real model calls).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

REQUESTS = (
    "Generate an image of a mountain at sunrise.",
    "Make me a picture of a cat asleep on a sofa.",
    "Create an image of a city skyline at night.",
)

#: Three apps, made-up names, deliberately different tool names — as real apps differ.
APPS = (("Pixelforge", "generate_image"), ("Artisan", "make_picture"), ("Lumina", "submit_image"))


def _server(app: str, tool: str, calls: list):
    from mcp.server import MCPServer

    server = MCPServer(f"stub-{app.lower()}")

    def make(prompt: str) -> str:
        calls.append(prompt)
        return f"Image ready: https://img.example/{app.lower()}/{len(calls)}.png"

    make.__name__ = tool
    server.tool(description="Generate an image from a text prompt and return its address.")(make)
    return server


class Stub:
    def __init__(self, app: str, tool: str, token: str):
        self.app, self.tool, self.token = app, tool, token
        self.calls: list = []
        self.auth_seen: list = []
        self.url = ""

    def __enter__(self):
        import socket
        import threading
        import time

        import uvicorn
        from stub_mcp_server import _RequireBearer

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        app = _RequireBearer(_server(self.app, self.tool, self.calls).streamable_http_app(),
                             self.token, self.auth_seen)
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=self._server.run, daemon=True).start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}/mcp"
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    from jarvis import identity_check, routing_check

    scratch = routing_check.prepare_scratch(identity_check.real_data_dir())
    (scratch / "data" / "connectors.json").unlink(missing_ok=True)   # only the stub apps exist
    print(f"{len(REQUESTS)} requests, each usually 2 to 5 model calls, in {scratch}")
    if not args.yes and input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1
    try:
        from jarvis.models import refresh_catalog

        refresh_catalog()
    except Exception as err:  # noqa: BLE001
        print(f"(model list not refreshed: {err})")

    from jarvis.config import save_secret
    from jarvis.connectors import capabilities, store

    stubs = [Stub(app, tool, f"token-{app.lower()}") for app, tool in APPS]
    for stub in stubs:
        stub.__enter__()
    try:
        for stub in stubs:
            save_secret(f"conn_{stub.app.lower()}", stub.token)
            connector = store.add_connector(type="mcp", label=stub.app, config={
                "connectFlow": {"kind": "token", "url": stub.url}, "secretRef": f"conn_{stub.app.lower()}"})
            store.update_connector(connector["id"], {"status": {"state": "working", "checkedAt": None,
                                                                "detail": None}})
            capabilities.refresh_tools(connector["id"])
            store.set_tool_permission(connector["id"], f"{stub.app.lower()}__{stub.tool}", "allow")

        duplicated = 0
        for request in REQUESTS:
            before = {s.app: len(s.calls) for s in stubs}
            reply = routing_check.run_scenario(routing_check.Scenario("duplicate check", request))
            made = {s.app: len(s.calls) - before[s.app] for s in stubs if len(s.calls) - before[s.app]}
            total = sum(made.values())
            verdict = "ok: made once" if total == 1 else (
                "FLAG: nothing was made" if total == 0 else f"FLAG: made {total} times")
            duplicated += total > 1
            print(f"\nYou:    {request}\nJarvis: {(reply.text or '(no reply)')[:300]}")
            print(f"  really called: {[c['name'] for c in reply.calls]}")
            print(f"  servers received: {made or 'nothing'}  ->  {verdict}")
        print(f"\n{duplicated} of {len(REQUESTS)} requests were made more than once.")
        return 0 if not duplicated else 2
    finally:
        for stub in stubs:
            stub.__exit__()


if __name__ == "__main__":
    sys.exit(main())
