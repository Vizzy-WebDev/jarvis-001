"""A small website on loopback, and a browser host that reaches it as if it were public.

Jarvis's browser refuses every private address, loopback included, and has no
switch to allow one (`jarvis.browser.network`). A test cannot therefore point it at
`127.0.0.1`. It does what OpenMuse's own tests do: names a made-up public host
(`site.test`), lets the REAL destination checks run against a resolver that says
it is a public address, and then - only after those checks have passed - has the
proxy's connector open the socket to the fixture instead. Everything between the
browser and that last step is the production code.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jarvis.browser import BrowserHost
from jarvis.browser.proxy import start_egress_proxy

#: A real public address (example.com's), used only as the answer our resolver gives.
PUBLIC_IP = "93.184.216.34"
HOST_NAME = "site.test"
BASE = f"http://{HOST_NAME}"

MAIN = b"""<!doctype html><html><head><title>Fixture Home</title></head><body>
<h1>Hello from the fixture site</h1>
<p id="status">nothing yet</p>
<button id="go" onclick="document.getElementById('status').textContent='the button was clicked'">Click me</button>
<input id="field" placeholder="your name">
<input id="secret" type="password" value="hunter2">
<select id="colour"><option>Red</option><option>Green</option></select>
<a id="two" href="/two">Second page</a>
<a id="blank" href="/two" target="_blank">Second page in a new tab</a>
<button id="alert" onclick="alert('hi'); document.getElementById('status').textContent='after alert'">Alert</button>
<button id="popup" onclick="window.open('/two')">Popup</button>
<script>
document.getElementById('field').addEventListener('keydown', function (e) {
  if (e.key === 'Enter') document.getElementById('status').textContent = 'submitted: ' + this.value;
});
</script></body></html>"""

TWO = b"<!doctype html><title>Second</title><body><p>This is page two.</p></body>"

STORAGE = b"""<!doctype html><title>Storage</title><body><p id="seen"></p><script>
const had = localStorage.getItem('kept');
document.getElementById('seen').textContent = 'had:' + had;
localStorage.setItem('kept', 'yes');
document.cookie = 'visited=1; max-age=3600; path=/';
</script></body>"""

LONG = ("<!doctype html><title>Long</title><body><pre>" + ("x" * 120_000) + "</pre></body>").encode()

TALL = ("<!doctype html><title>Tall</title><body style='margin:0'>"
        "<div style='height:3000px'>top</div><button id='low'>Low button</button></body>").encode()


#: A small but real PDF (header, one page, cross-reference table, end marker).
PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
       b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
       b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
       b"trailer<</Root 1 0 R/Size 4>>\nstartxref\n9\n%%EOF\n")

DOWNLOADS = b"""<!doctype html><title>Downloads</title><body>
<a id="pdf" href="/report.pdf">Download the report</a>
<a id="csv" href="/data.csv">Download the data</a>
<a id="fake" href="/fake.pdf">Download the fake</a>
<a id="big" href="/big.pdf">Download the huge one</a></body>"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    port = 0                            # set on the subclass
    slow_seconds = 13                   # how long /very-slow takes

    def log_message(self, *_args):
        pass

    def _send(self, body: bytes, kind: str = "text/html; charset=utf-8", status: int = 200,
              extra: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/":
            return self._send(MAIN)
        if path == "/two":
            return self._send(TWO)
        if path == "/storage":
            return self._send(STORAGE)
        if path == "/long":
            return self._send(LONG)
        if path == "/tall":
            return self._send(TALL)
        if path == "/downloads":
            return self._send(DOWNLOADS)
        if path == "/report.pdf":
            return self._send(PDF, "application/pdf",
                              extra={"Content-Disposition": 'attachment; filename="report.pdf"'})
        if path == "/inline.pdf":
            return self._send(PDF, "application/pdf")
        if path == "/data.csv":
            return self._send(b"a,b\n1,2\n", "text/csv",
                              extra={"Content-Disposition": 'attachment; filename="data.csv"'})
        if path == "/fake.pdf":
            return self._send(b"<html>this is not a pdf</html>", "application/pdf",
                              extra={"Content-Disposition": 'attachment; filename="fake.pdf"'})
        if path == "/big.pdf":
            return self._send(b"%PDF-1.4\n" + b"0" * (11 * 1024 * 1024), "application/pdf",
                              extra={"Content-Disposition": 'attachment; filename="big.pdf"'})
        if path == "/set-cookie":
            # An ordinary sign-in: a session cookie, no expiry, gone when a browser quits.
            return self._send(b"<!doctype html><title>Cookie set</title><body>set</body>",
                              extra={"Set-Cookie": "sid=signed-in; Path=/"})
        if path == "/cookie-echo":
            seen = (self.headers.get("Cookie") or "none").encode()
            return self._send(b"<!doctype html><title>Echo</title><body>cookie: " + seen + b"</body>")
        if path == "/slow-link":
            return self._send(b"<!doctype html><title>Slow link</title><body>"
                              b"<a id=go href='/very-slow'>Go to the slow page</a>"
                              b"<input id=f><form action='/very-slow' method=get>"
                              b"<input name=q id=q><button id=submit>Send</button></form></body>")
        if path == "/very-slow":
            # A server that takes a long time to answer: the click that asked for this page
            # has done its job long before the page arrives.
            import time

            time.sleep(self.slow_seconds)
            return self._send(b"<!doctype html><title>Finally</title><body>the slow page arrived</body>")
        if path == "/slow-body":
            # Headers and the start of the page arrive at once; the rest stalls. The page has
            # begun - a person would see it half drawn - but it never finishes in time.
            import time

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>Partial page</title><body><p>first part</p>")
            self.wfile.flush()
            time.sleep(self.slow_seconds)
            self.wfile.write(b"<p>last part</p></body>")
            self.close_connection = True
            return None
        if path == "/dialogs":
            return self._send(b"""<!doctype html><title>Dialogs</title><body>
<button id=alert onclick="alert('Heads up'); out.textContent='alert done'">Alert</button>
<button id=confirm onclick="out.textContent = 'confirm: ' + confirm('Delete everything?')">Confirm</button>
<button id=prompt onclick="out.textContent = 'prompt: ' + prompt('Your name?', 'nobody')">Prompt</button>
<p id=out>nothing yet</p></body>""")
        if path == "/frames":
            return self._send(b"""<!doctype html><title>Frames</title><body><h1>Outer page</h1>
<iframe id=inner name=inner src="/frame-inside" width=400 height=200></iframe></body>""")
        if path == "/frame-inside":
            return self._send(b"""<!doctype html><title>Inside</title><body><p>Text that lives inside the frame</p>
<input id=ff placeholder="card number"><button id=fb onclick="r.textContent='frame clicked'">Pay</button><p id=r></p></body>""")
        if path == "/popup-page":
            return self._send(b"""<!doctype html><title>Popup page</title><body>
<button id=pop onclick="window.open('/two', 'child'); out.textContent='opened a popup'">Sign in with a pop-up</button>
<p id=out></p></body>""")
        if path == "/to-private":
            return self._send(b"", status=302,
                              extra={"Location": f"http://127.0.0.1:{self.port}/"})
        if path == "/slow":
            import time

            time.sleep(2)
            return self._send(TWO)
        for hook in self.extra_routes:
            if hook(self, path):
                return None
        return self._send(b"not found", "text/plain", 404)

    extra_routes: list = []


class FixtureSite:
    """The server, plus a browser host that treats it as `http://site.test`."""

    def __init__(self) -> None:
        handler = type("Handler", (_Handler,), {"extra_routes": []})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        handler.port = self.port
        self.handler = handler
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = BASE
        #: Every address the resolver was asked about, in order - how a test proves the
        #: proxy connected to what was validated and did not look the name up again.
        self.lookups: list[str] = []

    def add_route(self, hook) -> None:
        """`hook(handler, path)` -> True if it answered."""
        self.handler.extra_routes.append(hook)

    async def resolve(self, name: str):
        self.lookups.append(name)
        if name == HOST_NAME:
            return [(PUBLIC_IP, 4)]
        raise OSError(f"no such host: {name}")

    async def connect(self, address: str, port: int, family: int):
        assert address == PUBLIC_IP, f"the proxy connected somewhere unvalidated: {address}"
        return await asyncio.open_connection("127.0.0.1", self.port)

    def host(self, directory: Path, **options) -> BrowserHost:
        return BrowserHost(
            directory=directory, resolve=self.resolve,
            make_proxy=lambda: start_egress_proxy(connect=self.connect, resolve=self.resolve),
            **options)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
