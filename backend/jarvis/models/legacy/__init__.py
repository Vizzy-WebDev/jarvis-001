"""The AI provider + model system.

A **connection** is access to one provider (an address, and a key where one is
needed). The **models** listed under it are what that connection can run. One
model is **selected**; that is the whole user-facing idea, and nothing here
grows it into a catalog, a set of per-model settings, or a router.

Three things are kept deliberately apart, because running them together is how a
system ends up saying something worked that did not:

* **Selected** — the person's own choice, stored as a preference. Only they
  ever change it.
* **Available** — whether that choice currently resolves to something that can
  be run. Worked out when asked, never stored.
* **Executed** — whether a request really ran on that model. Known only from the
  provider's own answer.

This package is imported piece by piece, never as a whole, on purpose:
`client.py` speaks the orchestrator's port and so drags the turn loop in with it,
while `oneshot.py` (behind `jarvis.ai`, which tools may call) must not. Nothing
here imports the tools, the capability executor, or the loader.
"""
