"""Memory — durable facts about the USER.

Distinct from Self-Improvement (what Jarvis has learned about its OWN
performance) and from chat history (what was said, and when). A fact is
recalled by being in the prompt; something said is recalled by being searched.
"""

from . import policy, store

__all__ = ["policy", "store"]
