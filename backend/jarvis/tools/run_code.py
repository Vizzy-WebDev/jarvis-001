"""Running code, described as it actually is.

The tool's own description is BUILT at load time from what the sandbox probe
found, so it can never claim isolation the machine does not have. That is the
whole point: in the original, the description promised a sandbox that the
`restricted` backend did not implement, and the model repeated the promise to
the user.

Risk follows the same fact. With real isolation this is MEDIUM — it changes
things, but only inside a container. Without it, running model-written code as
the user is HIGH, and gets a fresh confirmation every time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capabilities import CapabilityRegistry, CapabilitySpec, Risk
from ..sandbox import backend, describe_isolation, run_python


def _run(code: str = "", timeout_seconds: int = 30) -> dict[str, Any]:
    result = run_python(code, timeout_s=max(1.0, min(120.0, float(timeout_seconds or 30))))
    kept: list[dict[str, Any]] = []
    if result.ok and result.files:
        from ..artifacts import keep

        for produced in result.files:
            try:
                kept.append(keep(Path(produced)).as_result())
            except ValueError as err:
                # The file was produced and did not survive verification. Said
                # plainly rather than dropped, because "it made a file" and "it
                # made a file that opens" are different claims.
                kept.append({"name": Path(produced).name, "ok": False, "error": str(err)})

    return {
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exitCode": result.exit_code,
        "timedOut": result.timed_out,
        # Reported on every result, not just in the description: whoever reads
        # this should not have to remember what the machine supports.
        "isolation": result.backend,
        **({"files": kept} if kept else {}),
        **({"error": result.error} if result.error else {}),
    }


def _summary(args: dict[str, Any]) -> str:
    code = str(args.get("code") or "").strip()
    first = code.splitlines()[0][:70] if code else ""
    return (f"Run this Python on your computer, starting: {first}…\n{describe_isolation()}"
            if backend() != "wsl" else f"Run this Python in WSL, starting: {first}…")


def build(registry: CapabilityRegistry) -> list[CapabilitySpec]:
    isolated = backend() == "wsl"
    return [CapabilitySpec(
        id="builtin.run_code",
        name="run_code",
        description=("Run a short Python program and get its output. Useful for calculations, "
                     "converting data, or generating a file. " + describe_isolation()),
        input_schema={"type": "object", "properties": {
            "code": {"type": "string", "description": "The Python to run."},
            "timeout_seconds": {"type": "integer", "description": "Up to 120. Default 30."}},
            "required": ["code"]},
        # HIGH without a real boundary — see this module's docstring.
        risk=Risk.MEDIUM if isolated else Risk.HIGH,
        handler=_run,
        summarize=_summary,
        timeout_s=130.0,
    )]
