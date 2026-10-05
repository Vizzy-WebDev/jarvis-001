"""Probing: find out what an endpoint can really do by trying it — a tool round
trip, parallel tool calls, a strict schema, an image, streaming. The cases are
data (`data/probe_cases.yaml`). What is measured goes to the state file, never to
config, where it outranks both declared and discovered values.

A probe is a manual command (or a scheduled one), never part of a request:

    python -m jarvis.models.probe <connection/model-id> [--case NAME ...] [--yes]

Before probing anything that isn't known to be free it shows the estimated cost
and asks; `--yes` answers for a scheduled run that was set up knowingly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from . import config, drivers, engine, finish, state
from .catalog import Endpoint
from .errors import ModelError
from .execute import mint_id
from .discovery import conn_info
from .prepared import Prepared, PreparedOutput, RenderedSection
from .types import Finish, ImagePart, Message, TextDelta, TextPart, Tool, ToolCall, ToolCallCompleted, ToolResult

CASES_PATH = Path(__file__).resolve().parent / "data" / "probe_cases.yaml"


def load_cases() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8")) or {}
    return dict(data.get("cases") or {})


def estimate(endpoint: Endpoint, cases: dict[str, dict[str, Any]]) -> float | None:
    """US dollars, roughly, or None when the price isn't known."""
    if endpoint.pricing is None:
        return None
    tokens = sum(int(c.get("estimate_tokens") or 0) for c in cases.values())
    return endpoint.pricing.cost(input_tokens=int(tokens * 0.8), output_tokens=int(tokens * 0.2)) or 0.0


def _run(driver: Any, conn: Any, prepared: Prepared) -> tuple[list[Any], str, list[ToolCall], int]:
    events = list(driver.stream(conn, prepared))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    calls = [e.call for e in events if isinstance(e, ToolCallCompleted)]
    deltas = sum(1 for e in events if isinstance(e, TextDelta))
    return events, text, calls, deltas


def _case(name: str, case: dict[str, Any], endpoint: Endpoint, driver: Any, conn: Any) -> tuple[bool, str]:
    parts: list[Any] = [TextPart(case["prompt"])]
    if case.get("image"):
        parts.append(ImagePart(case["image"]["mime"], case["image"]["data_b64"]))
    items: tuple[Any, ...] = (Message("user", tuple(parts)),)
    tools: tuple[Tool, ...] = ()
    if case.get("tool"):
        t = case["tool"]
        tools = (Tool(t["name"], t.get("description", ""), driver.translate_schema(t["parameters"], conn.quirks)),)
    output = PreparedOutput()
    if case.get("schema"):
        output = PreparedOutput(mode="strict", schema=driver.translate_schema(case["schema"], conn.quirks))
    prepared = Prepared(endpoint_id=endpoint.id, model_id=endpoint.model_id,
                        system=(RenderedSection("task", "You are being tested. Follow the instruction exactly."),),
                        items=items, tools=tools, output=output, parallel_tools=True, mint_id=mint_id)
    events, text, calls, deltas = _run(driver, conn, prepared)
    want = case.get("pass") or {}
    if "streamed" in want:
        return deltas >= 1 and any(isinstance(e, Finish) for e in events), f"{deltas} text pieces"
    if "calls_at_least" in want:
        return len(calls) >= want["calls_at_least"], f"{len(calls)} tool calls in one step"
    if "valid" in want:
        value, problems = finish.check_output(text, case["schema"])
        return not problems, "matched the schema" if not problems else "; ".join(problems)
    if "called" in want:
        called = [c for c in calls if c.name == want["called"]]
        if not called:
            return False, "it didn't call the tool"
        assembled = finish.Assembled()
        for event in events:
            assembled.add(event, endpoint.id)
        follow = (*items, *assembled.items,
                  *[ToolResult(c.id, c.name, case.get("tool_result")) for c in called])
        again = Prepared(**{**prepared.__dict__, "items": follow})
        _, reply, _, _ = _run(driver, conn, again)
        ok = str(want.get("reply_contains", "")).lower() in reply.lower()
        return ok, "the round trip worked" if ok else f"after the tool result it said: {reply[:120]!r}"
    if "reply_contains" in want:
        ok = str(want["reply_contains"]).lower() in text.lower()
        return ok, f"it said: {text[:80]!r}"
    return False, "this case has no pass rule"


def probe(endpoint_id: str, *, cases: list[str] | None = None,
          confirm: Callable[[str], bool] | bool = False) -> dict[str, Any]:
    """Run the cases; record what passed and failed as probed capabilities."""
    cfg = config.current()
    cat = engine.catalog(cfg)
    endpoint = cat.endpoints.get(endpoint_id)
    if endpoint is None:
        raise ValueError(f"There's no endpoint called “{endpoint_id}”.")
    chosen = {k: v for k, v in load_cases().items() if not cases or k in cases}
    free = endpoint.pricing is not None and endpoint.pricing.free
    if not free:
        cost = estimate(endpoint, chosen)
        question = (f"Probing {endpoint_id} runs {len(chosen)} small requests, costing about ${cost:.4f}. Go ahead?"
                    if cost is not None else
                    f"Probing {endpoint_id} runs {len(chosen)} small requests, and its price isn't known. Go ahead?")
        agreed = confirm(question) if callable(confirm) else confirm
        if not agreed:
            return {"endpoint": endpoint_id, "ran": False, "message": "Not probed: it wasn't confirmed."}
    conn_cfg = cat.connections[endpoint.connection]
    driver = drivers.get(conn_cfg.driver)
    conn = conn_info(conn_cfg, cfg)
    measured: dict[str, Any] = {}
    results: dict[str, Any] = {}
    for name, case in chosen.items():
        try:
            ok, detail = _case(name, case, endpoint, driver, conn)
        except ModelError as err:
            ok, detail = False, f"{err.type}: {err}"
            if err.type in ("auth", "unavailable", "timeout", "rate_limited"):
                results[name] = {"ok": None, "detail": detail}  # says nothing about the capability
                continue
        results[name] = {"ok": ok, "detail": detail}
        measured[case["capability"]] = ok
    state.record_probe(endpoint_id, measured, results)
    return {"endpoint": endpoint_id, "ran": True, "capabilities": measured, "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.models.probe",
                                     description="Try an endpoint's capabilities and record what works.")
    parser.add_argument("endpoint", help="connection/model-id")
    parser.add_argument("--case", action="append", help="only this case (repeatable)")
    parser.add_argument("--yes", action="store_true", help="don't ask before probing a paid endpoint")
    args = parser.parse_args(argv)

    def ask(question: str) -> bool:
        print(question, "[y/N] ", end="", flush=True)
        return sys.stdin.readline().strip().lower() in ("y", "yes")

    result = probe(args.endpoint, cases=args.case, confirm=True if args.yes else ask)
    print(json.dumps(result, indent=2))
    state.flush()
    return 0 if result.get("ran") else 1


if __name__ == "__main__":
    raise SystemExit(main())
