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
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
    """The loader imports every tool module, so an import back is a cycle. It
    deadlocks and presents as a hung server with no error at all."""
    assert offending(files_under("tools"), (
        "jarvis.tools.__init__", "jarvis.capabilities.execute",
        "jarvis.orchestrator",
        # The composition root imports the loader, so reaching it from a tool is
        # the same edge one hop further out — and it looks perfectly innocent.
        "jarvis.assembly",
    )) == []


def test_a_tool_gets_the_registry_passed_in_rather_than_reaching_for_it():
    """`from ..capabilities import registry` — the shared singleton — is the
    edge that becomes a cycle. Importing the TYPES is fine."""
    bad = []
    # The loader itself is not a tool: it is the thing holding the registry,
    # and it is what passes it in.
    for path in (p for p in files_under("tools") if p.name != "__init__.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "capabilities" in (node.module or ""):
                bad += [f"{path.name} imports the shared registry singleton"
                        for alias in node.names if alias.name == "registry"]
    assert bad == []


def test_a_tool_asks_a_model_through_the_narrow_seam():
    """`jarvis/ai.py` exists to be the one thing a tool may ask a model through.

    It preserves a real boundary rather than a naming preference: a tool should
    say "answer this", not drive a stream. The seam itself must stay clear of the
    loader, or it becomes the cycle it was added to avoid.
    """
    assert offending([PACKAGE / "ai.py"], (
        "jarvis.tools", "jarvis.capabilities", "jarvis.orchestrator.pipeline",
    )) == []


def test_a_skills_ui_reads_a_source_that_cannot_return_a_built_in():
    """The permanent Skills rule, asserted rather than remembered.

    A built-in ability must never be offered as an installable Skill. That has
    regressed repeatedly in the design this replaces, every time by someone
    listing capabilities and filtering — so the routes read the folder list,
    which structurally cannot return one, and this checks they still do.
    """
    tree = ast.parse((PACKAGE / "routes" / "skills.py").read_text(encoding="utf-8"))
    listing = {node.name: node for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in ("installed", "detail")}
    assert set(listing) == {"installed", "detail"}, "the listing routes were renamed"

    for name, node in listing.items():
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        # Either the folder list itself or the single-folder read built on it —
        # both live in the skills store and neither can return a built-in.
        assert names & {"list_user_skills", "get_skill"}, \
            f"{name}() must read the folder list"
        for reaching in ("registry", "get_registry", "declarations", "load_tools"):
            assert reaching not in names, (
                f"{name}() reaches for {reaching!r} — a Skills listing must read the "
                "folder list, which cannot return a built-in, never a capability list")


# --- the authority ceiling ---------------------------------------------------

def test_the_policy_layer_stays_pure():
    """§7 requires permissions enforced independently of model behaviour. A
    policy module that can reach the executor or a tool is a policy
    module that can be argued with."""
    assert offending([p for p in files_under("policy") if p.name != "approvals.py"], (
        "jarvis.capabilities.execute", "jarvis.orchestrator", "jarvis.tools",
    )) == []


def test_the_capability_registry_does_not_know_about_authorization():
    """Composition and authorization are different concerns. Keeping them in one
    file is what let a visibility policy and a confirm-token service end up
    inside the thing that answers 'what capabilities exist'."""
    assert offending([PACKAGE / "capabilities" / "registry.py"], ("jarvis.policy",)) == []


# --- the orchestrator's restraint --------------------------------------------

def test_the_turn_loop_imports_no_subsystem_that_watches_it():
    """The loop must not know about cost, the self-model, improvement or
    tracing. A loop importing such modules cannot be
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


def test_the_orchestrator_does_not_import_a_provider_sdk():
    """It talks to the model port. A turn loop that knows a wire format is a
    turn loop a second one has to be written to avoid."""
    for path in files_under("orchestrator"):
        source = path.read_text(encoding="utf-8")
        for sdk in ("import openai", "import anthropic", "from google import genai"):
            assert sdk not in source, f"{path.name} reaches for a provider SDK"


# --- leaf modules ------------------------------------------------------------

#: The two places a child process legitimately gets the real environment: both
#: hand a target to the user's OWN desktop shell or browser. That child is the
#: user's application — not model-written code, and not a program a connector
#: template names — and it needs the real environment to behave normally.
DESKTOP_LAUNCHERS = ("jarvis/tools/open_app.py", "jarvis/tools/_http.py")


def test_every_child_process_is_given_an_environment_deliberately():
    """Saving an API key writes it into this process's environment, so a child
    that inherits `os.environ` inherits every key the user has configured. The
    sandbox got this right and the CLI connector did not — the difference was
    invisible until someone went looking, which is what this test replaces."""
    offences = []
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE.parent).as_posix()
        if relative in DESKTOP_LAUNCHERS:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (target.attr if isinstance(target, ast.Attribute)
                    else target.id if isinstance(target, ast.Name) else "")
            if name not in ("run", "Popen") or not isinstance(target, ast.Attribute):
                continue
            if getattr(target.value, "id", "") != "subprocess":
                continue
            if not any(kw.arg == "env" for kw in node.keywords):
                offences.append(f"{relative}:{node.lineno} launches a child with no env=")
    assert offences == []


@pytest.mark.parametrize("leaf", ["store.py", "config.py", "jscompat.py"])
def test_the_persistence_leaves_stay_leaves(leaf):
    """Everything imports these; if they import anything back, nothing can be
    loaded in isolation."""
    assert imports_of(PACKAGE / leaf) == set()


# --- the provider and model system ---------------------------------------------

def test_only_the_turn_loops_client_imports_the_orchestrator():
    """`models/client.py` speaks the orchestrator's port, so it drags the whole turn
    loop in behind it. Everything else in the package must stay clear of that, or a
    tool asking a question through `jarvis.ai` would be led into the loop — the
    edge the loader invariant exists to keep out."""
    assert offending([p for p in files_under("models") if p.name != "client.py"], (
        "jarvis.orchestrator",
    )) == []


def test_the_model_system_reaches_for_nothing_it_would_form_a_cycle_with():
    assert offending(files_under("models"), (
        "jarvis.tools", "jarvis.capabilities", "jarvis.assembly", "jarvis.routes",
    )) == []


def test_a_tool_never_reaches_the_turn_loops_model_client():
    """Tools may ask a model through `ai.ask` — which runs on the one-shot path — and
    never through the client, which is the orchestrator's."""
    assert offending(files_under("tools") + [PACKAGE / "ai.py"], ("jarvis.models.client",)) == []


def test_the_turn_loop_knows_no_provider_and_no_connection():
    """It streams from a port. Which provider is behind it is not its business,
    and an import of the model system here is how that stops being true."""
    assert offending(files_under("orchestrator"), ("jarvis.models",)) == []


def test_the_model_rows_are_a_leaf_over_the_database():
    allowed = ("jarvis.db", "jarvis.jscompat")
    assert [n for n in imports_of(PACKAGE / "models" / "store.py")
            if not n.startswith(allowed)] == []


def test_a_provider_module_knows_its_own_wire_and_nothing_else_of_jarvis():
    """One module per format, and each speaks only its own. A provider that reached
    into the store, the selection or another provider would be a gateway."""
    allowed = ("jarvis.models.errors", "jarvis.models.types", "jarvis.models.providers",
               "jarvis.conversation", "jarvis.prompt_format", "jarvis.redact")
    for path in files_under("models", "providers"):
        stray = [n for n in imports_of(path) if not n.startswith(allowed)]
        assert stray == [], f"{path.name} reaches for {stray}"
    formats = [p for p in files_under("models", "providers") if p.name not in ("__init__.py", "_wire.py")]
    for path in formats:
        for other in formats:
            if other != path:
                assert f"jarvis.models.providers.{other.stem}" not in imports_of(path), \
                    f"{path.name} imports {other.name}"


def test_no_provider_name_is_compared_in_the_shared_layers():
    """Provider-specific behaviour lives in that provider's module. The selection,
    the client and the routes never branch on which company it is."""
    shared = [PACKAGE / "models" / n for n in ("selection.py", "client.py", "runtime.py", "oneshot.py")]
    shared.append(PACKAGE / "routes" / "models.py")
    for path in shared:
        source = path.read_text(encoding="utf-8")
        for brand in ('"openai"', '"anthropic"', '"gemini"', "'openai'", "'anthropic'", "'gemini'"):
            assert brand not in source, f"{path.name} compares a provider name ({brand})"


# --- specialist agents ----------------------------------------------------------

def test_the_turn_loop_never_imports_the_agents_package():
    """An agent's turn is an ordinary turn given a brief (`AgentBrief`, plain data
    defined in `orchestrator/`). The loop takes it as data and never reaches into
    `jarvis.agents` — so there is no second loop for specialists to grow into."""
    assert offending(files_under("orchestrator"), ("jarvis.agents",)) == []


def test_no_tool_reaches_the_agent_runner():
    """Delegation is registered from `assembly.py`, after the loader, like Skills and
    connectors. A module under `tools/` importing the runner would pull the turn
    loop in behind it: the loader invariant's cycle, one hop further out."""
    assert offending(files_under("tools"), (
        "jarvis.agents.runner", "jarvis.agents.capabilities",
    )) == []


def test_the_agent_store_stays_a_leaf_over_the_database():
    assert imports_of(PACKAGE / "agents" / "store.py") <= {"jarvis.db", "jarvis.db.get_db",
                                                             "jarvis.jscompat",
                                                             "jarvis.jscompat.now_iso"}
