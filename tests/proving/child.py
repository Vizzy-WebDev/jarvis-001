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
        # A provider that does not answer at that moment is rested for minutes, and a run
        # started then would fail in seconds for a reason that is nothing to do with Jarvis
        # (found live). So: wait until a model would actually take a chat turn, or give up
        # saying so.
        import time

        if os.environ.get("PROVING_AUTO") == "1":
            # Auto model selection for this run: the copied settings lose their "selected"
            # model (in the throw-away copy only), so a turn can move to another connected
            # model when one stops answering — as the owner asked for the proving runs.
            try:
                from jarvis.models import config as model_config

                model_config.remove_alias("selected")
                print("(running under Auto: no selected model)", flush=True)
            except Exception as err:  # noqa: BLE001 — then it runs as the settings stand
                print(f"(could not switch this run to Auto: {err})", flush=True)

        for attempt in range(6):
            try:
                from jarvis.models import refresh_catalog
                from jarvis.models.client import JarvisModelClient

                refresh_catalog()
                if JarvisModelClient().context_capacity(session_id="proving-check") is not None:
                    break
                print(f"(no model would answer yet — try {attempt + 1}; waiting)", flush=True)
            except Exception as err:  # noqa: BLE001 — the copied list is still there to try with
                print(f"(could not refresh the model list: {err})", flush=True)
            time.sleep(30)

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
