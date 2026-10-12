"""A real Jarvis, in its own process, for tests that must use the real thing.

Used by `test_browser_live.py`: a real server (uvicorn, the real app) on a port of its own,
the person's own model configuration (a COPY of `models.yaml`; keys are READ from the real
`.env`, never written), a scratch data directory so nothing of theirs is touched, and the
chat spoken to over the real HTTP stream exactly as the interface does it.

What these helpers read back is what the model DID - the stored tool calls and results in the
scratch database - not what it said it did. A model's account of an action is not evidence.

Approvals are answered one at a time, by capability name, and only after the turn that asked
has ended (as the interface does). Nothing is ever approved blindly: a capability that is not
named is left unanswered.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

REPO = Path(__file__).resolve().parent.parent
REAL_ENV = REPO / ".env"
REAL_MODELS = REPO / "data" / "models.yaml"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass
class Reply:
    text: str = ""
    tools: list[tuple[str, bool, str | None]] = field(default_factory=list)   # (name, ok, error)
    approvals: list[tuple[str, str]] = field(default_factory=list)            # (capability, decision)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    failed: str | None = None
    seconds: float = 0.0

    def used(self, name: str) -> bool:
        return any(tool[0] == name for tool in self.tools)


class LiveApp:
    """The real app, started and stopped as a person's own would be."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None
        self.log = data_dir.parent / f"{data_dir.name}-server.log"

    # --- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not (self.data_dir / "models.yaml").exists():
            (self.data_dir / "models.yaml").write_bytes(REAL_MODELS.read_bytes())
        env = dict(os.environ)
        env.update(JARVIS_DATA_DIR=str(self.data_dir), JARVIS_ENV_PATH=str(REAL_ENV),
                   PORT=str(self.port))
        env.pop("JARVIS_FRONTEND_DIR", None)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO / "server"), env.get("PYTHONPATH", "")]).strip(os.pathsep)
        code = ("import uvicorn; from jarvis.main import create_app; "
                f"uvicorn.run(create_app(), host='127.0.0.1', port={self.port}, log_level='warning')")
        self.process = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=str(REPO),
                                        stdout=self.log.open("ab"), stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.base}/api/status", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if self.process.poll() is not None:
                raise RuntimeError(f"the server exited: see {self.log}")
            time.sleep(0.4)
        raise RuntimeError(f"the server never answered: see {self.log}")

    def stop(self) -> None:
        if self.process is None:
            return
        # Closing the app must also close its browser; give it the chance a real quit has.
        self.process.terminate()
        try:
            self.process.wait(20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.process = None

    def restart(self) -> None:
        self.stop()
        time.sleep(3)                       # let the old browser let go of its profile
        self.start_again()

    def process_kill(self) -> None:
        """The console window closed: no goodbye, nothing gets to save anything."""
        if self.process is not None:
            self.process.kill()
            self.process.wait(20)
            self.process = None
        time.sleep(6)                       # the browser notices its driver has gone

    def start_again(self) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.start()

    # --- talking ------------------------------------------------------------------------

    def new_chat(self) -> None:
        httpx.post(f"{self.base}/api/conversations", timeout=20).raise_for_status()

    def say(self, message: str, *, allow: Iterable[str] = (), deny: Iterable[str] = (),
            follow_up: str = "Go on.", max_follow_ups: int = 0) -> Reply:
        """Send one message. Approvals the turn asks for are answered after it ends:
        capabilities in `allow` are allowed, in `deny` are denied, any other is left alone.
        If something was allowed, the person's next words carry the model on (`follow_up`),
        up to `max_follow_ups` times - which is how the interface works."""
        allow, deny = set(allow), set(deny)
        combined = Reply()
        text = message
        for round_number in range(max_follow_ups + 1):
            reply, asked = self._turn(text)
            combined.text += ("\n" if combined.text else "") + reply.text
            combined.tools += reply.tools
            combined.attachments += reply.attachments
            combined.failed = reply.failed or combined.failed
            combined.seconds += reply.seconds
            allowed_any = False
            for approval_id, capability in asked:
                if capability in allow:
                    self._decide(approval_id, "allow")
                    combined.approvals.append((capability, "allow"))
                    allowed_any = True
                elif capability in deny:
                    self._decide(approval_id, "deny")
                    combined.approvals.append((capability, "deny"))
                else:
                    combined.approvals.append((capability, "left"))
            if not allowed_any or round_number == max_follow_ups:
                break
            text = follow_up
        return combined

    def _decide(self, approval_id: str, decision: str) -> None:
        httpx.post(f"{self.base}/api/approvals/{approval_id}", json={"decision": decision},
                   timeout=120).raise_for_status()

    def _turn(self, message: str) -> tuple[Reply, list[tuple[str, str]]]:
        reply = Reply()
        asked: list[tuple[str, str]] = []
        started = time.time()
        with httpx.stream("GET", f"{self.base}/api/chat/stream", params={"message": message},
                          timeout=httpx.Timeout(300, connect=20)) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip() or "{}")
                kind = data.get("type")
                if kind == "chunk":
                    reply.text += data.get("text", "")
                elif kind == "tool_result":
                    reply.tools.append((data.get("capability"), bool(data.get("ok")),
                                        data.get("error")))
                    reply.attachments += data.get("attachments") or (
                        [data["attachment"]] if data.get("attachment") else [])
                elif kind == "approval_required":
                    asked.append((data["approvalId"], data["capability"]))
                elif kind == "failed":
                    reply.failed = data.get("error")
        reply.seconds = time.time() - started
        return reply, asked

    # --- what really happened ------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{(self.data_dir / 'jarvis.db').as_posix()}?mode=ro", uri=True)

    def steps(self, since: int = 0) -> list[dict[str, Any]]:
        """Every stored message in order: role, text, tool calls and tool results."""
        try:
            with self._db() as db:
                rows = db.execute("SELECT role, text, payload FROM messages WHERE id > ? "
                                  "ORDER BY id", (since,)).fetchall()
        except sqlite3.OperationalError:          # nothing has been said yet: no database
            return []
        out = []
        for role, text, payload in rows:
            body = json.loads(payload) if payload else {}
            out.append({"role": role, "text": text or "",
                        "calls": [(c.get("name"), c.get("args")) for c in body.get("toolCalls", [])],
                        "results": body.get("toolResults", [])})
        return out

    def mark(self) -> int:
        """A place in the conversation record: results asked for `since` it are only the ones
        made after it. Without this a test reads an EARLIER test's result when the model, this
        time, did not use the tool at all."""
        try:
            with self._db() as db:
                row = db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0])

    def results_of(self, name: str, since: int = 0) -> list[dict[str, Any]]:
        """The stored result of every call to one tool, oldest first."""
        found = []
        for step in self.steps(since):
            for result in step["results"]:
                if isinstance(result, dict) and result.get("name") == name:
                    value = result.get("result")
                    found.append(value if isinstance(value, dict) else {"raw": value})
        return found

    def batching(self) -> dict[str, Any]:
        """How well the model obeyed "call browser tools one at a time".

        `risky` is the harmful kind: an action that needs the person's go-ahead sent together
        with another browser call, so the other call runs first and reports the OLD page."""
        used, together, risky = 0, [], []
        for step in self.steps():
            names = [name for name, _ in step["calls"] if str(name).startswith("browser_")]
            if not names:
                continue
            used += 1
            if len(names) > 1:
                together.append(names)
                if {"browser_click", "browser_type"} & set(names):
                    risky.append(names)
        return {"steps_using_browser_tools": used, "sent_together": together, "risky": risky}

    def keep_evidence(self, destination: Path) -> None:
        """Copy what the run left (database, sessions' small files), minus the browser profile."""
        import shutil

        shutil.copytree(self.data_dir, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("profile", "*.log", "Cache*"))

    def window(self, session: str = "0a7e5d11-6a72-4a76-8a1c-000000000001") -> dict[str, Any]:
        return httpx.get(f"{self.base}/api/browsers/{session}", timeout=20).json()

    def read_window(self, session: str = "0a7e5d11-6a72-4a76-8a1c-000000000001") -> dict[str, Any]:
        return httpx.get(f"{self.base}/api/browsers/{session}/read", timeout=60).json()

    def read_window_when_ready(self, session: str = "0a7e5d11-6a72-4a76-8a1c-000000000001",
                               seconds: float = 60) -> dict[str, Any]:
        """The window's page once it has finished loading - a slow site is not a failed test."""
        deadline = time.time() + seconds
        page: dict[str, Any] = {}
        while time.time() < deadline:
            page = self.read_window(session)
            if page.get("ok") and not page.get("loading") and page.get("text"):
                return page
            time.sleep(1.5)
        return page

    def wait_idle(self, seconds: float = 150, quiet: float = 3.0) -> None:
        """Wait until the model has finished the turn: the last thing stored is its own answer,
        with no tool call waiting on a result, and nothing new has been stored for `quiet` s."""
        deadline = time.time() + seconds
        last_count, since = -1, time.time()
        while time.time() < deadline:
            steps = self.steps()
            if len(steps) != last_count:
                last_count, since = len(steps), time.time()
            done = bool(steps) and steps[-1]["role"] == "assistant" and not steps[-1]["calls"]
            if done and time.time() - since >= quiet:
                return
            time.sleep(0.5)

    def navigate_text(self, message: str, mark_name: str = "browser_navigate",
                      tries: int = 3) -> str:
        """Ask the model to open something and return what the browser really showed. A site
        that answers with a 5xx page (httpbin does, now and then) is asked again: that is the
        website's weather, not a finding."""
        text = ""
        for _ in range(tries):
            before = self.mark()
            self.say(message)
            results = self.results_of(mark_name, before)
            text = results[-1].get("text", "") if results else ""
            if not text.startswith(("502", "503", "504", "Service", "Bad Gateway")):
                return text
        return text

    def artifacts(self) -> list[dict[str, Any]]:
        return httpx.get(f"{self.base}/api/artifacts", timeout=20).json()["artifacts"]
