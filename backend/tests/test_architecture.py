"""Fitness functions: the seams, asserted rather than remembered.

Every rule here exists because its absence caused a real, documented problem —
a deadlock, a bypassed permission check, a subsystem that could not be tested
because it dragged five others in with it. Comments do not hold a boundary; a
failing test does.

The check is a static import scan, not a runtime one: an edge that only appears
on some code path is still an edge, and waiting for it to be exercised is how it
gets found in production instead of here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "jarvis"


def imports_of(path: Path) -> set[str]:
    """Every module this file imports, as dotted `jarvis.` names."""
    tree = ast.parse(path.read_text(), filename=str(path))
    package = path.relative_to(PACKAGE.parent).with_suffix("").parts
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import: resolve it against this file
                base = package[:-node.level] if node.level <= len(package) else ()
                prefix = ".".join(base)
                head = f"{prefix}.{node.module}" if node.module else prefix
            else:
                head = node.module or ""
            found.add(head)
            found.update(f"{head}.{alias.name}" for alias in node.names)
    return {name for name in found if name.startswith("jarvis")}


def files_under(*parts: str) -> list[Path]:
    return sorted((PACKAGE.joinpath(*parts)).rglob("*.py"))


def offending(files: list[Path], forbidden: tuple[str, ...]) -> list[str]:
    out = []
    for path in files:
        for name in imports_of(path):
            if any(name == f or name.startswith(f + ".") for f in forbidden):
                out.append(f"{path.relative_to(PACKAGE.parent)} imports {name}")
    return out


# --- the loader invariant ----------------------------------------------------

def test_no_tool_imports_the_loader_or_the_things_built_on_it():
    """The loader imports every tool module, so an import back is a cycle. In
    the Node original this deadlocked and presented as a hung server with no
    error at all."""
    assert offending(files_under("tools"), (
        "jarvis.tools.__init__", "jarvis.capabilities.execute",
        "jarvis.orchestrator", "jarvis.gateway",
    )) == []


def test_a_tool_gets_the_registry_passed_in_rather_than_reaching_for_it():
    """`from ..capabilities import registry` — the shared singleton — is the
    edge that becomes a cycle. Importing the TYPES is fine."""
    bad = []
    # The loader itself is not a tool: it is the thing holding the registry,
    # and it is what passes it in.
    for path in (p for p in files_under("tools") if p.name != "__init__.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "capabilities" in (node.module or ""):
                bad += [f"{path.name} imports the shared registry singleton"
                        for alias in node.names if alias.name == "registry"]
    assert bad == []


# --- the authority ceiling ---------------------------------------------------

def test_the_policy_layer_stays_pure():
    """§7 requires permissions enforced independently of model behaviour. A
    policy module that can reach the executor, the gateway or a tool is a policy
    module that can be argued with."""
    assert offending([p for p in files_under("policy") if p.name != "approvals.py"], (
        "jarvis.capabilities.execute", "jarvis.orchestrator", "jarvis.gateway", "jarvis.tools",
    )) == []


def test_the_capability_registry_does_not_know_about_authorization():
    """Composition and authorization are different concerns. Keeping them in one
    file is what let a visibility policy and a confirm-token service end up
    inside the thing that answers 'what capabilities exist'."""
    assert offending([PACKAGE / "capabilities" / "registry.py"], ("jarvis.policy",)) == []


# --- the orchestrator's restraint --------------------------------------------

def test_the_turn_loop_imports_no_subsystem_that_watches_it():
    """The loop must not know about cost, the self-model, improvement or
    tracing. The Node turn loop imported six such modules and could not be
    tested without them.

    Scoped to the LOOP rather than the whole package, deliberately: assembling
    context legitimately READS stores (it already reads memory), and a rule that
    cannot tell "reads a store to build the prompt" from "is called to record
    what happened" would forbid the wrong thing. The recorders themselves are
    forbidden package-wide, just below.
    """
    assert offending([PACKAGE / "orchestrator" / "pipeline.py"], (
        "jarvis.cost", "jarvis.improvement", "jarvis.self", "jarvis.ops",
        "jarvis.observers",
    )) == []


def test_nothing_in_the_orchestrator_calls_a_recorder():
    """The other direction: a capability publishes what it did and a subscriber
    writes it down. Anything under here reaching for a recorder means the event
    seam has quietly stopped being the mechanism."""
    assert offending(files_under("orchestrator"), (
        "jarvis.improvement.capture", "jarvis.self.capture", "jarvis.observers",
        "jarvis.cost", "jarvis.ops",
    )) == []


def test_the_orchestrator_does_not_import_a_provider_sdk_or_an_adapter():
    """It talks to the model port. A turn loop that knows a wire format is a
    turn loop a second one has to be written to avoid."""
    assert offending(files_under("orchestrator"), ("jarvis.adapters",)) == []
    for path in files_under("orchestrator"):
        source = path.read_text()
        for sdk in ("import openai", "import anthropic", "from google import genai"):
            assert sdk not in source, f"{path.name} reaches for a provider SDK"


def test_only_adapters_import_provider_sdks():
    """One place per wire format. Two callers in the Node app bypassed the
    adapter layer entirely to construct a provider SDK directly, which is how
    the voice path ended up with none of the gateway's protections."""
    for path in PACKAGE.rglob("*.py"):
        if path.parent.name == "adapters":
            continue
        source = path.read_text()
        for sdk in ("from openai import", "from anthropic import", "from google import genai"):
            assert sdk not in source, f"{path.relative_to(PACKAGE.parent)} imports a provider SDK"


# --- leaf modules ------------------------------------------------------------

@pytest.mark.parametrize("leaf", ["store.py", "config.py", "jscompat.py"])
def test_the_persistence_leaves_stay_leaves(leaf):
    """Everything imports these; if they import anything back, nothing can be
    loaded in isolation."""
    assert imports_of(PACKAGE / leaf) == set()
