# Cost tracking — `self_awareness/cost/`

See the root `CLAUDE.md` for the decisions that matter beyond this file. This is the
module-by-module breakdown.

## The one rule every module here answers to

**Three separately-labelled kinds of number, never blended.** `store.py`'s three tables ARE
this rule, structurally:
- `cost_events` — **measured**. What was actually counted; always available, never estimated.
- `provider_balances` — **provider-reported**. A real figure from a provider's own account
  API, only where a reader is registered (none is at the moment; see `balances.py`).
- `model_prices` — used only to **calculate** a dollar figure from a measured count.
  `prices.calculate()` returns nothing (never a guess) for a model with no price on record.

**"Free" and "no price known" are different answers.** A `$0` row is a real price: it counts
toward the total, and `report.py` lists the group as free. No row at all means nobody knows:
the group is excluded from the total and named in `pricelessGroups`. Collapsing the two
would either understate spend or make a `$0` month look like a month with no data.

## Recording

- `store.py` is a leaf (imports only `db.py` and the time helper), so a tool may import it
  and a recording failure stays confined to recording. `session_id` is deliberately not a
  foreign key: a cost event must survive the conversation that produced it being deleted.
- **Model calls:** `observers/cost.py`'s `record_model_call()` subscribes to the model-call
  event on the bus — the turn loop never imports cost tracking. It records only events
  carrying `usage` (the orchestrator publishes the same event type once per step without
  usage, and recording both would count every turn twice) and only with a `provider` to
  attribute it to.
- **TTS** records at its own call site, `tts/__init__.py`, as `unit_kind="characters"` — most
  providers bill per character requested, not per chunk played. Speech-to-text is not
  recorded.

## `prices.py` — a price is read, never stored

There is no price table. `calculate()` finds the model layer's own endpoint for the ledger's
provider name and model id and multiplies by its `Pricing` (per million tokens): what the person
wrote in their model settings, else what the connection's own list reported, else $0 for a
connection that runs on their own machine. Non-token units and unknown prices give `None`.
Nothing refreshes and nothing can disagree with the model settings. The `provider` key must be
the same one the recorder uses (`Connection.ledger_name`).

**No dollar figure is hardcoded for a cloud model** — a plausible-looking invented number is
exactly the failure this subsystem exists to prevent. The recorder is fed by
`models/runtime.py`'s `publish_completed`, which publishes `provider` and the usage the
provider itself reported (tokens in, out, cached) and nothing when it reported nothing. No
provider exposes a reliable balance, so there is no balance ledger (migration 39 dropped
`provider_balances` and `model_prices`).

## `report.py` + `tools/check_spending.py`

`report.py` is the read side: `usage_breakdown(since_iso)` groups by provider, model and unit
kind, attaches a calculated dollar figure only where a price is known, and separately lists
`pricelessGroups` and `freeGroups`, so an answer can say plainly "X has no known price".
`today()`, `month_to_date()`, `last_days()` and `most_used()` build on it.
`tools/check_spending.py` (read-only, no confirmation) is the whole user-facing interface:
a conversational tool, with no dashboard screen.
