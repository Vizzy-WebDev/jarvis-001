"""Building a TLS context costs about a second on some machines, and httpx builds a fresh one for every
client or one-off call unless handed one. Every place that calls out must hand it the shared one."""

from __future__ import annotations

import ast
from pathlib import Path

import jarvis

_CALLS = {"Client", "AsyncClient", "get", "post", "put", "delete", "request", "stream", "patch"}


def _sources():
    for root in jarvis.__path__:
        yield from Path(root).rglob("*.py")


def test_the_shared_context_is_built_once():
    from jarvis import tls

    assert tls.context() is tls.context()


def test_no_http_call_in_the_app_builds_its_own_tls_context():
    offenders = []
    for path in _sources():
        if path.name == "_wire.py":  # the model layer keeps its own (it may import nothing of the app)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "httpx"
                    and node.func.attr in _CALLS):
                if not any(k.arg in ("verify", "transport") for k in node.keywords):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders
