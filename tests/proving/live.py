"""Prove what Jarvis does on its own, against the person's real model.

    python tests/proving/live.py --real-root <the folder Jarvis runs from> [--only short,trap]
                                 [--repeat 3] [--out <folder>] [--yes]

Each run gets a throw-away folder: only the model settings, preferences and keys are COPIED in
(`models.yaml`, `models_state.json`, `prefs.json`, `.env`), so it uses the same models and
touches nothing real. Each phase of a scenario is its own process, which is what lets a
scenario kill Jarvis mid-work and have a fresh process take over. The outcome is read from
what the run left on disk (`score.py`), never from what Jarvis said about itself.

Every model call made is counted and reported, because they are the person's quota.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tests/proving
TESTS = HERE.parent
REPO = TESTS.parent
sys.path[:0] = [str(REPO / "server"), str(TESTS)]

COPIED = ("models.yaml", "models_state.json", "prefs.json")


def prepare(real_root: Path, scratch: Path) -> None:
    """A fresh folder holding copies of the model settings and keys, and nothing else real."""
    real_data = (real_root / "data").resolve()
    assert scratch.resolve() != real_data and real_data not in scratch.resolve().parents, \
        "refusing to run inside the real data folder"
    (scratch / "data").mkdir(parents=True, exist_ok=True)
    for name in COPIED:
        if (real_data / name).exists():
            shutil.copy2(real_data / name, scratch / "data" / name)
    env = real_root / ".env"
    if env.exists():
        shutil.copy2(env, scratch / ".env")
    else:
        (scratch / ".env").write_text("", encoding="utf-8")


#: Set by `--tools-in-hand` for every child this run starts.
IN_HAND = False


def _child(scenario_id: str, phase: str, scratch: Path, seed: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["PROVING_TOOLS_IN_HAND"] = "1" if IN_HAND else "0"
    env["JARVIS_DATA_DIR"] = str(scratch / "data")
    env["JARVIS_ENV_PATH"] = str(scratch / ".env")
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "server"), str(TESTS), env.get("PYTHONPATH", "")])
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen([sys.executable, str(HERE / "child.py"), scenario_id, phase,
                             str(scratch), str(seed)], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")


def kill_hard(child: subprocess.Popen) -> None:
    """Kill a child and everything under it, with no chance to clean up. On Windows a virtual
    environment's `python.exe` is a small launcher that starts the real interpreter as its own
    child: killing only the launcher (found the hard way — 28 of them had piled up) leaves the
    real process running, which for a "kill it mid-work" proof means nothing was killed."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        child.kill()


def _run_phase(scenario, phase: str, scratch: Path, seed: int, kill: bool) -> str:
    """Run one phase to its end — or, for a kill, until it says it reached the point to be
    killed at. Returns "ended", "killed" or "timed out"."""
    from proving.harness import KILL_MARK

    child = _child(scenario.id, phase, scratch, seed)
    marked = threading.Event()
    lines: list[str] = []

    def read() -> None:
        assert child.stdout is not None
        for line in child.stdout:
            lines.append(line.rstrip())
            if KILL_MARK in line:
                marked.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + scenario.deadline_s + 120
    how = "ended"
    while child.poll() is None:
        if kill and marked.is_set():
            kill_hard(child)        # no clean-up, no goodbye: exactly what a crash is
            how = "killed"
            break
        if time.monotonic() > deadline:
            kill_hard(child)
            how = "timed out"
            break
        time.sleep(0.2)
    child.wait(30)
    reader.join(5)
    (scratch / f"output-{phase}.txt").write_text("\n".join(lines), encoding="utf-8")
    return how


def run_once(scenario, real_root: Path, out: Path, seed: int, label: str) -> dict:
    from proving import harness

    scratch = out / label
    prepare(real_root, scratch)
    kill = scenario.fault is not None and scenario.fault.kind == "kill"
    how = _run_phase(scenario, "start", scratch, seed, kill)
    if kill and how == "killed":
        how = _run_phase(scenario, "resume", scratch, seed, kill=False)
    verdict = harness.judge(scenario, scratch)
    verdict["run"] = label
    if kill and not (scratch / "output-resume.txt").exists():
        verdict["passed"] = False
        verdict["problems"].append("the process was never killed mid-work, so nothing was proved")
    if how == "timed out":
        verdict["passed"] = False
        verdict["problems"].append("the run did not end in time")
    (scratch / "verdict.json").write_text(json.dumps(verdict, indent=1), encoding="utf-8")
    return verdict


def line_for(verdict: dict) -> str:
    mark = "PASS" if verdict["passed"] else "FAIL"
    extra = "; ".join(verdict["violations"] + verdict["problems"])
    return (f"{mark}  {verdict['run']:<18} {verdict['outcome']:<16} (wanted {verdict['expect']}) "
            f"calls={verdict['modelCalls']} asked={verdict['questionsAsked']} "
            f"{verdict['seconds']:.0f}s — {verdict['why']}" + (f" — {extra}" if extra else ""))


def summary(verdicts: list[dict]) -> str:
    by: dict[str, list[dict]] = {}
    for verdict in verdicts:
        by.setdefault(verdict["scenario"], []).append(verdict)
    lines = ["", "scenario        passed   outcomes                              calls  what it proves"]
    for scenario_id, runs in by.items():
        outcomes = ", ".join(sorted({r["outcome"] for r in runs}))
        lines.append(f"{scenario_id:<15} {sum(r['passed'] for r in runs)}/{len(runs):<6} {outcomes:<37} "
                     f"{sum(r['modelCalls'] for r in runs):<6} {runs[0]['proves']}")
    lines.append("")
    lines.append(f"{sum(v['passed'] for v in verdicts)} of {len(verdicts)} runs passed. "
                 f"False 'done': {sum(v['outcome'] == 'false_done' for v in verdicts)}. "
                 f"Hard-rule violations: {sum(bool(v['violations']) for v in verdicts)}. "
                 f"Model calls made: {sum(v['modelCalls'] for v in verdicts)} "
                 f"({sum(v['modelCallsAnswered'] for v in verdicts)} answered).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from proving.scenarios import BY_ID, SCENARIOS

    parser = argparse.ArgumentParser(description="Prove what Jarvis does on its own, live.")
    parser.add_argument("--real-root", required=True,
                        help="the folder Jarvis runs from (holds data/ and .env)")
    parser.add_argument("--only", default="", help="comma-separated scenario ids (default: all)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", default="")
    parser.add_argument("--seed", type=int, default=0, help="0 = a different world every run")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--tools-in-hand", action="store_true",
                        help="declare the world's tools on every turn, instead of leaving Jarvis "
                             "to find them: tests the work itself, not the finding")
    args = parser.parse_args(argv)
    global IN_HAND
    IN_HAND = args.tools_in_hand

    real_root = Path(args.real_root).resolve()
    wanted = [s for s in args.only.split(",") if s] or [s.id for s in SCENARIOS]
    unknown = [s for s in wanted if s not in BY_ID]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}. Known: {', '.join(BY_ID)}")
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="jarvis-proving-"))
    out.mkdir(parents=True, exist_ok=True)
    runs = len(wanted) * args.repeat
    print(f"{runs} run(s) against your real models, each in its own throw-away folder under {out}. "
          "Your real data is not touched. Each run is typically 5 to 40 model calls.")
    if not args.yes and input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing was run.")
        return 1

    verdicts: list[dict] = []
    for scenario_id in wanted:
        for n in range(1, args.repeat + 1):
            seed = args.seed or int(time.time() * 1000) % 1_000_000
            verdict = run_once(BY_ID[scenario_id], real_root, out, seed, f"{scenario_id}-{n}")
            verdicts.append(verdict)
            print(line_for(verdict), flush=True)
            (out / "report.json").write_text(json.dumps(verdicts, indent=1), encoding="utf-8")
    text = summary(verdicts)
    print(text)
    (out / "report.txt").write_text("\n".join(line_for(v) for v in verdicts) + "\n" + text,
                                    encoding="utf-8")
    return 0 if all(v["passed"] for v in verdicts) else 2


if __name__ == "__main__":
    sys.exit(main())
