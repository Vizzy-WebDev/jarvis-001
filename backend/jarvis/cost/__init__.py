"""Cost tracking: what was actually used, what a provider says is left, and
what that works out to in money — three different kinds of number, never
blended into one.

* **Measured** (`cost_events`) — counts this build made itself, from what the
  provider's own response reported. Always available.
* **Provider-reported** (`provider_balances`) — a real figure from a provider's
  own account API. Only where one exists.
* **Calculated** (measured x a known price) — `None`, never a guess, for
  anything with no price on record.

The separation is the point. A single "you have spent $X" that silently mixes a
counted number with an invented price is worse than three honest numbers, one of
which is missing.
"""
