"""Proving what Jarvis can do on its own — a small world, objectives, and an honest score.

- `world.py`     a records office whose state and every action are on disk.
- `scenarios.py` the objectives, what goes wrong on purpose, and what the right ending is.
- `harness.py`   runs one scenario through the real Jarvis; judges it from what it left behind.
- `score.py`     reads a finished run's folder — none of Jarvis's own code, none of its claims.
- `live.py`      runs scenarios against the person's real model, in a throw-away folder.

The scripted tests (`tests/test_proving.py`) use the same world, harness and scorer with a
stand-in model: they prove the machinery. `live.py` proves the behaviour.
"""
