"""A real, minimal OAuth 2.1 + PKCE authorization server plus a matching
protected MCP endpoint — for testing `jarvis.connectors.oauth` against a real
HTTP round trip rather than mocking the module under test.

Scriptable: `no_registration=True` omits `registration_endpoint`, simulating a
server that does not support Dynamic Client Registration. `declares_iss=True`
advertises `authorization_response_iss_parameter_supported`, the RFC 9207
signal that makes a missing `iss` on callback a real rejection rather than a
tolerated absence.

PKCE is verified for real: a code is only redeemable by whoever holds the
verifier that produced its challenge, exactly as a real authorization server
enforces it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class StubOAuthServer:
    def __init__(self, *, no_registration: bool = False, declares_iss: bool = True,
                require_auth: bool = True):
        self.no_registration = no_registration
        self.declares_iss = declares_iss
        self.require_auth = require_auth
        self.registered_clients: dict[str, dict] = {}
        self._issued_codes: dict[str, dict] = {}
        self._server: HTTPServer | None = None
        self.base_url = ""

    def start(self) -> str:
        server_self = self
        issued = self._issued_codes
        registered = self.registered_clients

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: A003 — quiet, real server logs would drown test output
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802 — http.server's own method name
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""

                if parsed.path == "/mcp":
                    if server_self.require_auth and not self.headers.get("Authorization"):
                        self.send_response(401)
                        self.send_header(
                            "WWW-Authenticate",
                            f'Bearer resource_metadata="{server_self.base_url}'
                            '/.well-known/oauth-protected-resource"')
                        self.end_headers()
                        return
                    self._json({"jsonrpc": "2.0", "id": 0, "result": {"tools": []}})
                    return

                if parsed.path == "/register":
                    client_id = f"client-{len(registered) + 1}"
                    registered[client_id] = json.loads(raw)
                    self._json({"client_id": client_id})
                    return

                if parsed.path == "/token":
                    form = parse_qs(raw.decode())
                    grant = form.get("grant_type", [""])[0]
                    if grant == "authorization_code":
                        code = form.get("code", [""])[0]
                        entry = issued.pop(code, None)  # a real server's code is one-time too
                        if not entry:
                            self._json({"error": "invalid_grant"}, 400)
                            return
                        verifier = form.get("code_verifier", [""])[0]
                        challenge = base64.urlsafe_b64encode(
                            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                        if challenge != entry["challenge"]:
                            self._json({"error": "invalid_grant",
                                       "error_description": "PKCE verification failed"}, 400)
                            return
                        self._json({"access_token": "tok-initial", "refresh_token": "refresh-1",
                                   "expires_in": 3600, "token_type": "Bearer"})
                        return
                    if grant == "refresh_token":
                        self._json({"access_token": "tok-refreshed", "expires_in": 3600})
                        return
                    self._json({"error": "unsupported_grant_type"}, 400)
                    return

                self._json({}, 404)

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/.well-known/oauth-protected-resource":
                    # A server that genuinely needs no authorization at all
                    # (require_auth=False) would not publish this document
                    # either — oauth.py treats PUBLISHING it as a protected
                    # resource declaring itself as such, regardless of what an
                    # unauthenticated request answered, so serving it
                    # unconditionally here would misrepresent an open server.
                    if not server_self.require_auth:
                        self._json({}, 404)
                        return
                    self._json({"resource": f"{server_self.base_url}/mcp",
                               "authorization_servers": [server_self.base_url]})
                    return
                if parsed.path == "/.well-known/oauth-authorization-server":
                    meta = {"issuer": server_self.base_url,
                           "authorization_endpoint": f"{server_self.base_url}/authorize",
                           "token_endpoint": f"{server_self.base_url}/token",
                           "code_challenge_methods_supported": ["S256"]}
                    if not server_self.no_registration:
                        meta["registration_endpoint"] = f"{server_self.base_url}/register"
                    if server_self.declares_iss:
                        meta["authorization_response_iss_parameter_supported"] = True
                    self._json(meta)
                    return
                if parsed.path == "/authorize":
                    qs = parse_qs(parsed.query)
                    code = f"code-{len(issued) + 1}"
                    issued[code] = {"challenge": qs["code_challenge"][0]}
                    # A real authorization server renders a consent page and then
                    # redirects; this hands back exactly what a browser would have
                    # been redirected WITH, so a test can drive the callback route
                    # itself without a real browser.
                    self._json({"code": code, "state": qs["state"][0]})
                    return
                self._json({}, 404)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
