"""Scheduled tasks and the briefing.

A scheduled task runs on a clock the assistant has no judgement about — distinct
from a background Job, which is work it (or the user) chose to background right
now. Keeping them separate is what stops "run this at 8am" and "keep working on
that" from collapsing into one mechanism that does neither well.
"""

from . import briefing_config, engine, recurrence, task_store

__all__ = ["briefing_config", "engine", "recurrence", "task_store"]
