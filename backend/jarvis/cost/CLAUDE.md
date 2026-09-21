# Cost tracking — `jarvis/cost/`

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

## `prices.py` — write-time precedence

1. A price the user set (`set_user_price()`) always wins and is never silently replaced.
2. A provider's own published numeric pricing — `refresh_from_openrouter()` pulls OpenRouter's
   public per-token prices and skips any model whose price is already `source: user`.

**No dollar figure is hardcoded for a cloud model** — a plausible-looking invented number is
exactly the failure this subsystem exists to prevent. The `provider` key must be the same
one the recorder uses; deriving it a second way writes rows nothing reads.

The network refresh (`start_price_maintenance()`) and balance polling (`balances.start()`,
`JARVIS_COST_REFRESH`) are plain periodic timers behind their interlocks. They are
deliberately NOT heartbeat sources: the heartbeat spends a model call judging whether a
finding is worth interrupting for, and a silent maintenance refresh has no finding.

## Reading prices back

`advisor.py`'s `observed_cost_tier()` buckets a recorded price into a 0-4 scale and says
"no opinion" when none exists, so an unpriced model is neither rewarded nor punished. Nothing
ranks models on it at the moment; there is no model system to do so.

## `report.py` + `tools/check_spending.py`

`report.py` is the read side: `usage_breakdown(since_iso)` groups by provider, model and unit
kind, attaches a calculated dollar figure only where a price is known, and separately lists
`pricelessGroups` and `freeGroups`, so an answer can say plainly "X has no known price".
`today()`, `month_to_date()`, `last_days()` and `most_used()` build on it.
`tools/check_spending.py` (read-only, no confirmation) is the whole user-facing interface:
a conversational tool, with no dashboard screen.
