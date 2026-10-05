"""An optional fixed, ordered pipeline in a Skill folder: `skill.toml`.

**Parsed with the standard library's own TOML reader.** The original hand-wrote
a narrow TOML subset because its runtime has none, and documented that as a
dependency decision it did not want to make. Python has `tomllib`, so this build
takes the real parser: the grammar is no longer a thing that can be subtly wrong,
and an unsupported construct is a real parse error rather than a silent
misreading. Validation stays strict and separate — a mis-parsed instruction file
merely displays wrong, but a mis-parsed pipeline EXECUTES.

Two step kinds and nothing else: `tool` (dispatched through the injected
executor, so every existing gate applies exactly as it would to any other call)
and `prompt` (one model call, no tools, no side effects). Sequential, fixed
order, no branching.

**The executor is injected, never imported.** This module lives under the Skills
package, and reaching the capability seam from here would be exactly the cycle
the leaf rule exists to prevent.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable

from .templates import Unresolved, extract_refs, resolve

logger = logging.getLogger(__name__)

MAX_STEPS = 20
VALID_INPUT_TYPES = ("string", "number", "boolean")
DEFAULT_STEP_TIMEOUT_S = 30.0


@dataclass
class Pipeline:
    description: str | None = None
    inputs: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)


def parse(text: str) -> Pipeline:
    """Plain data out of `skill.toml`. Raises with the reason on bad TOML."""
    try:
        data = tomllib.loads(str(text or ""))
    except tomllib.TOMLDecodeError as err:
        raise ValueError(f"skill.toml could not be read: {err}") from err

    inputs = data.get("inputs") or []
    steps = data.get("steps") or []
    if not isinstance(inputs, list) or not isinstance(steps, list):
        raise ValueError("skill.toml: [[inputs]] and [[steps]] must each be a list of tables.")
    description = data.get("description")
    return Pipeline(description=description if isinstance(description, str) else None,
                    inputs=[i for i in inputs if isinstance(i, dict)],
                    steps=[s for s in steps if isinstance(s, dict)])


def validate(pipeline: Pipeline, *, known_tools: set[str] | None = None) -> list[str]:
    """Every problem, in plain language. An empty list means it is runnable.

    `known_tools` is passed IN rather than looked up: this module must not reach
    the capability seam, and a validator that imports it would be the cycle.
    """
    errors: list[str] = []

    if len(pipeline.steps) > MAX_STEPS:
        errors.append(f"This pipeline has {len(pipeline.steps)} steps — the limit is "
                      f"{MAX_STEPS}.")

    input_names: set[str] = set()
    for index, declared in enumerate(pipeline.inputs, start=1):
        name = declared.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f'Input {index}: missing "name".')
            continue
        if name in input_names:
            errors.append(f'Input {index}: duplicate input name "{name}".')
        input_names.add(name)
        kind = declared.get("type")
        if kind is not None and kind not in VALID_INPUT_TYPES:
            errors.append(f'Input "{name}": unsupported type "{kind}" — use string, number '
                          f"or boolean.")

    step_index: dict[str, int] = {}
    for index, step in enumerate(pipeline.steps):
        step_id = step.get("id")
        label = f'Step "{step_id}"' if isinstance(step_id, str) and step_id \
            else f"Step {index + 1}"
        if not isinstance(step_id, str) or not step_id.strip():
            errors.append(f'{label}: missing "id".')
        elif step_id in step_index:
            errors.append(f'{label}: duplicate id "{step_id}" (already used by an earlier '
                          f"step).")
        else:
            step_index[step_id] = index

        tool = step.get("tool")
        prompt = step.get("prompt")
        has_tool = isinstance(tool, str) and tool.strip()
        has_prompt = isinstance(prompt, str) and prompt.strip()
        if has_tool and has_prompt:
            errors.append(f'{label}: has both "tool" and "prompt" — a step is one kind or '
                          f"the other.")
        elif not has_tool and not has_prompt:
            errors.append(f'{label}: has neither "tool" nor "prompt".')
        elif has_tool and known_tools is not None and tool not in known_tools:
            errors.append(f'{label}: unknown tool "{tool}".')

    for index, step in enumerate(pipeline.steps):
        step_id = step.get("id")
        label = f'Step "{step_id}"' if isinstance(step_id, str) and step_id \
            else f"Step {index + 1}"
        references = extract_refs(step.get("args") or {})
        if isinstance(step.get("prompt"), str):
            references += extract_refs(step["prompt"])
        for reference in references:
            if reference.root == "inputs":
                if reference.id not in input_names:
                    errors.append(f'{label}: references "{{{{{reference.ref}}}}}" but no '
                                  f'input named "{reference.id}" is declared.')
            elif reference.root == "steps":
                if reference.id not in step_index:
                    errors.append(f'{label}: references "{{{{{reference.ref}}}}}" but no '
                                  f'step has id "{reference.id}".')
                elif step_index[reference.id] >= index:
                    errors.append(f'{label}: references "{{{{{reference.ref}}}}}" but step '
                                  f'"{reference.id}" has not run yet — a step can only '
                                  f"reference an EARLIER one.")
            else:
                errors.append(f'{label}: "{{{{{reference.ref}}}}}" is not a valid reference '
                              f"— use inputs.<name> or steps.<id>.<path>.")
    return errors


def confirm_requiring_steps(pipeline: Pipeline, *,
                            needs_confirmation: Callable[[str], bool]) -> list[str]:
    """Which tool steps would need a human. The whole pipeline confirms ONCE, up
    front, naming these — rather than pausing halfway through on step three."""
    return [str(step.get("tool")) for step in pipeline.steps
            if isinstance(step.get("tool"), str) and needs_confirmation(step["tool"])]


@dataclass
class StepResult:
    id: str
    ok: bool
    kind: str
    result: Any = None
    error: str | None = None


def run(pipeline: Pipeline, *, invoke: Callable[[str, dict[str, Any]], Any],
        ask: Callable[[str], Any], inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the steps in order, stopping at the first failure.

    Stopping rather than continuing is deliberate: a later step referencing an
    earlier one would otherwise run against a missing value, and a pipeline that
    half-ran is far harder to reason about than one that stopped where it broke.
    """
    context: dict[str, Any] = {"inputs": dict(inputs or {}), "steps": {}}
    results: list[StepResult] = []

    for step in pipeline.steps[:MAX_STEPS]:
        step_id = str(step.get("id") or f"step{len(results) + 1}")
        try:
            if isinstance(step.get("tool"), str) and step["tool"].strip():
                args = resolve(step.get("args") or {}, context)
                value = invoke(step["tool"], args if isinstance(args, dict) else {})
                outcome = StepResult(id=step_id, ok=True, kind="tool", result=value)
            else:
                prompt = resolve(step.get("prompt") or "", context)
                value = ask(str(prompt))
                outcome = StepResult(id=step_id, ok=True, kind="prompt", result=value)
        except Unresolved as err:
            outcome = StepResult(id=step_id, ok=False, kind="unknown", error=str(err))
        except Exception as err:  # noqa: BLE001 — a step failing is data, not a crash
            logger.info("pipeline step %s failed: %s", step_id, err)
            outcome = StepResult(id=step_id, ok=False,
                                 kind="tool" if step.get("tool") else "prompt", error=str(err))

        results.append(outcome)
        context["steps"][step_id] = {"ok": outcome.ok, "result": outcome.result}
        if not outcome.ok:
            break

    ok = bool(results) and all(r.ok for r in results)
    return {
        "ok": ok,
        "steps": [{"id": r.id, "ok": r.ok, "kind": r.kind, "result": r.result,
                   "error": r.error} for r in results],
        "result": results[-1].result if ok and results else None,
        "error": next((r.error for r in results if not r.ok), None),
    }
