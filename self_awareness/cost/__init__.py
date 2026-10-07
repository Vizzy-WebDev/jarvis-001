"""Cost tracking: what was actually used, and what that works out to in money — two
different kinds of number, never blended into one.

* **Measured** (`cost_events`) — counts this build made itself, from what the
  provider's own response reported. Always available.
* **Calculated** (measured x a known price, the price being the model layer's own
  for that endpoint) — `None`, never a guess, for anything with no price on record.

The separation is the point. A single "you have spent $X" that silently mixes a
counted number with an invented price is worse than two honest numbers, one of
which may be missing.
"""
