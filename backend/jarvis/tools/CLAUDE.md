# Tools (`jarvis/tools/*.py`)

Real, executable capabilities — `get_weather`, `open_app`, `run_code`, `schedule_task`, ... —
as distinct from a Skill (`jarvis/skills/`), which is a folder of *instructions* with no code
of its own. See the root `CLAUDE.md` for the PERMANENT RULE governing what may ever appear
as a Skill in the UI. This file covers the architecture of `jarvis/tools/` itself.

## How a tool is registered

`tools/__init__.py`'s `load_tools()` imports every non-underscore module here and registers
its module-level `SPEC` (one tool), `SPECS` (several related tools), or the result of
`build(registry)` (a tool that needs the registry itself — handed to it, never reached for).
To add a tool: a new module in `jarvis/tools/`, nothing else to touch. A tool is one
`CapabilitySpec` (`jarvis/capabilities/spec.py`):

```python
SPEC = CapabilitySpec(
    id="builtin.get_time",          # unique; the registry refuses a collision
    name="get_time",                # what the model calls
    description="...",              # what the model reads to decide when to call it
    input_schema={...},             # JSON Schema
    risk=Risk.LOW,                  # REQUIRED, no default
    handler=_run,
    timeout_s=30.0,                 # enforced by the executor for every caller
    retry=RetryPolicy(attempts=0),  # HIGH risk + auto-retry is refused at construction
    tags=frozenset({"core"}),       # see "Tags" below
    summarize=lambda args: "...",   # the read-back sentence when approval is needed
    redact_args=frozenset(),        # argument names hidden from logs, events and the UI
    wants_context=False,            # True if the handler also needs the CallContext
)
```

**`risk` is required and has no default**: a capability cannot be treated as safe because
someone forgot to classify it, and the permission layer decides from this declared value
rather than inferring per call. `RetryPolicy` refuses `attempts > 0` on a `HIGH` risk
capability at construction, which is how one "send message" stops being able to become three.

`handler` returns plain data; the model phrases the spoken reply. A handler with
`wants_context=True` also receives the `CallContext` (session, turn, autonomy — and what a
Skill running a pipeline needs to invoke other capabilities).

**Several related tools share one module** — `self_tools.py` holds `check_myself` and
`track_goal`, `memory_tools.py` holds the five memory tools, and so on.

## The import invariant

Nothing under `jarvis/tools/` may import the loader, the executor or the orchestrator,
directly or transitively: `load_tools()` imports every module here, so
an import back is a cycle that deadlocks and looks like a hung server. Import heavy
dependencies lazily inside the handler. A tool needing something only the registry can answer
gets it through `build(registry)`. `tests/test_architecture.py` asserts this.

`skill_tools.py` and its neighbours import `jarvis/skills/files.py` — a legitimate,
one-directional `tools/` → `skills/` dependency on a dependency-free leaf, never the loader.

## Execution and approval (`capabilities/`, `policy/`)

`capabilities/` merges tools, folder Skills and connector tools into one declaration list;
`capabilities/execute.py` is the one dispatcher (timeout, retry, argument validation,
idempotency on `operation_id`, redaction, `tool.*` events). A tool file is never called
directly from outside `capabilities/`.

**Approval is decided by `policy/decide.py` from the declared risk, never by the model.**
LOW runs. MEDIUM needs approval, except inside a task the user pre-consented to
(`Autonomy.PRE_CONSENTED`). HIGH always needs a human unless a standing grant names that exact
capability; a wildcard grant never covers HIGH, and pre-consent never covers HIGH. Voice is
recorded as a `Surface` and never relaxes anything. Low speech confidence can only add a
confirmation. `allowed_names` (a task's allowlist) is enforced here too.

When approval is needed, `policy/approvals.py` records an `approvals` row storing the exact
arguments described (so approving executes what was shown, and the model cannot alter them
between asking and running) and the tool result asks the person via `summarize()`. The user
answers through `routes/approvals.py`, which resolves the row and runs
`execute_approved()`. **A confirmation may only be resolved by a LATER turn than the one that
asked**: `resolve()` raises `SameTurnRefused` otherwise, which is structural — a model told to
"skip asking" cannot mint and answer its own question. A call outside the per-turn system
always carries a fresh `CallContext`; `session_id` and `turn_id` are required so this cannot
be silently skipped.

## Tags

- `core` — always declared to the model. Above `DECLARATION_BUDGET` (20) the turn declares
  only `core` tools, ones unlocked this turn, and any explicitly allowed by name.
  `find_capability` is how the rest become reachable: it returns matches under an `unlock`
  key and the orchestrator adds them for the rest of that turn.
- `job` — a background worker's own voice (reporting on itself, asking for its work to be
  split). Declared only on a job's turn, never in a live conversation.
- `meta` — abilities that only make sense in live conversation (scheduling, memory
  management, notifications, self-inspection). `skills/capabilities.py` leaves them out of the
  tools a Skill pipeline may name.

## `ui_action` on a tool result

The general way to make the browser DO something beyond showing text; do not invent a second
mechanism. `orchestrator/pipeline.py` reads it in exactly two shapes:
`{type: 'navigate', section}` (`open_section.py`) and `{type: 'attachment', kind: 'image' |
'video' | ..., url, mimeType}` (`take_screenshot.py`, `screen_recording.py`). The `url` must be
a real, already-servable route such as `/api/control/screenshots/:file`, never a data URI. To
hand the user a downloadable file, use an artifact (`jarvis/artifacts/CLAUDE.md`).

## Gotchas

**Check a result with `result.get("ok") is False`, not `not result.get("ok")`** — a tool with
nothing to report beyond success (`get_time`) returns no `ok` key at all.

**Tools that touch the OS use an allowlist, never raw shell strings.** `open_app.py` maps a
friendly name to a fixed command (`ALLOWLIST`, plus Start Menu shortcut lookup) launched as
`subprocess.Popen(["cmd.exe", "/c", "start", "", target])` — list arguments, so model text
never reaches a shell. `open_website.py` only opens `http(s)` URLs.

**A tool that needs the list of reserved Skill names cannot import it** — the tool is loaded
by the registry's loader, and a top-level import back into `capabilities/` would deadlock.
Receive what you need through `build(registry)`.
