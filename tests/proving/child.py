"""One phase of one scenario, in its own process — started (and for a kill, killed) by `live.py`.

    python child.py <scenario id> <start|resume> <scratch folder> <seed>

The environment must already point at the scratch folder: this refuses to run otherwise, so
it can never reach the person's real data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    scenario_id, phase, scratch, seed = argv[0], argv[1], Path(argv[2]).resolve(), int(argv[3])
    data = os.environ.get("JARVIS_DATA_DIR")
    assert data and Path(data).resolve() == (scratch / "data").resolve(), \
        "refusing to run: JARVIS_DATA_DIR is not this run's scratch folder"
    assert os.environ.get("JARVIS_ENV_PATH"), "refusing to run without a scratch .env"

    from proving import harness
    from proving.scenarios import BY_ID

    scenario = BY_ID[scenario_id]
    if phase == "start":
        # The app finds its models at startup, which this process never runs: ask the
        # connections what they offer now (a model list, not a model call).
        try:
            from jarvis.models import refresh_catalog

            refresh_catalog()
        except Exception as err:  # noqa: BLE001 — the copied list is still there to try with
            print(f"(could not refresh the model list: {err})", flush=True)

    def kill_point() -> None:
        print(harness.KILL_MARK, flush=True)

    result = harness.play(scenario, scratch, seed=seed, phase=phase, on_kill_point=kill_point)
    print(f"phase {phase} ended: settled={result.get('settled')} error={result.get('error')}",
          flush=True)
    return 0


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.stdout.flush()
    # Daemon threads (a model call still in flight, the supervisor's timer) must not keep a
    # finished phase alive.
    os._exit(code)
