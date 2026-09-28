"""Conformance: nothing a single provider says leaks out of its own module.

Provider-specific status codes, error-body fields and header names may appear only in
`models/providers/` (the adapters and their shared wire helper) and `models/gateways/`
(a declared gateway's own listing fields). Checked on the CODE — string and number
literals, not comments or docstrings, which may explain what an adapter does."""

from __future__ import annotations

import ast
from pathlib import Path

JARVIS = Path(__file__).resolve().parents[2] / "jarvis"
MODELS = JARVIS / "models"

#: Words that only make sense in one provider's own error bodies, headers or listings.
PROVIDER_TOKENS = ("overloaded_error", "rate_limit_error", "authentication_error", "invalid_request_error",
                   "insufficient_quota", "context_length_exceeded", "rate_limit_exceeded", "invalid_api_key",
                   "RESOURCE_EXHAUSTED", "PERMISSION_DENIED", "INVALID_ARGUMENT", "UNAVAILABLE",
                   "API_KEY_INVALID", "RetryInfo", "retryDelay", "x-ratelimit", "retry-after",
                   "owned_by", "combo", "input_modalities", "output_modalities", "supported_parameters",
                   "architecture", "pricing", "thinkingBudget", "output_config", "reasoning_effort")
#: HTTP statuses a provider answers with — reading them is a provider module's job.
PROVIDER_STATUSES = {400, 401, 402, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504, 529}


def shared_modules() -> list[Path]:
    """Everything in the model system that is NOT an adapter or a gateway reader."""
    return [p for p in MODELS.rglob("*.py")
            if "providers" not in p.parts and "gateways" not in p.parts]


def literals(path: Path) -> list[tuple[int, object]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)}
    # A slice bound (`text[:400]`) is a length, never a status.
    bounds = {id(part) for node in ast.walk(tree) if isinstance(node, ast.Slice)
              for part in (node.lower, node.upper, node.step) if part is not None}
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and id(node) not in docstrings | bounds]


def test_no_provider_error_field_or_header_is_read_outside_the_adapters():
    found = []
    for path in shared_modules() + [JARVIS / "routes" / "models.py"]:
        for line, value in literals(path):
            if isinstance(value, str):
                found += [f"{path.name}:{line} {token!r}" for token in PROVIDER_TOKENS if token in value]
    assert found == []


def test_no_provider_status_code_is_compared_outside_the_adapters():
    found = [f"{path.name}:{line} {value}" for path in shared_modules() for line, value in literals(path)
             if isinstance(value, int) and not isinstance(value, bool) and value in PROVIDER_STATUSES]
    assert found == []


def test_the_router_decides_nothing_from_a_failures_kind_except_the_billing_exemption():
    """Scope is the adapter's call. The routing code may read `err.scope`; the one place it
    may read `kind` is "needs credit", which passes over models listed as free."""
    for name in ("attempt.py", "auto.py", "health.py"):
        tree = ast.parse((MODELS / name).read_text(encoding="utf-8"))
        compared = [node for node in ast.walk(tree) if isinstance(node, ast.Compare)
                    and any(isinstance(n, ast.Attribute) and n.attr == "kind" for n in ast.walk(node.left))]
        for node in compared:
            constants = [c.value for c in ast.walk(node) if isinstance(c, ast.Constant)]
            assert constants == ["billing"], f"{name}:{node.lineno} decides on kind {constants}"
