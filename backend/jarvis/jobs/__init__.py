"""Background work: jobs the assistant keeps doing while the user talks about
something else.

Distinct from the scheduler, which runs on a clock nobody judges. A job is work
that was chosen — by the assistant or the user — to continue in the background
right now, and it needs supervision, recovery and an escalation path that a
scheduled task does not.
"""

from . import job_store, orchestrator, policy, worker

__all__ = ["job_store", "orchestrator", "policy", "worker"]
