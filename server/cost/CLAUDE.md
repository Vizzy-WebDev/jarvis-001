# Cost tracking — `server/cost/*.js`

See the root `CLAUDE.md`'s "Operational Awareness" section for the decisions that matter
beyond this file (items 2/Cost-tracking and 6/cost-at-decision-time). This file is the
module-by-module breakdown.

## The one rule every module here answers to

**Three separately-labelled kinds of number, never blended, per the owner's own explicit
requirement.** `cost-store.js`'s three tables ARE this rule, structurally:
- `cost_events` — **measured**. What was actually counted, always available, never
  estimated.
- `provider_balances` — **provider-reported**. A real figure straight from a provider's
  own account API, only where one exists (ElevenLabs `/v1/user`, OpenRouter `/v1/key`).
- `model_prices` — used only to **calculate** a dollar figure from a measured count;
  `report.js`'s `calculate()` returns `null` (never a guess) for anything with no known
  price on record.

## `cost-store.js` (leaf) + `record.js`

`cost-store.js` is the raw table access, same discipline as every other `*-store.js` in
this project. `record.js` is the thin, always-safe seam every real call site actually
uses (`recordModelUsage`/`recordTtsUsage`/`recordSttUsage`) — each wrapped in its own
try/catch that logs and swallows, never rethrows, since a cost-recording failure must
never break the turn/call it's measuring (same principle `self/self-capture.js`'s
`recordAttempt()` already established for the Self-Model's own recorder).

## The usage seam — three adapters, one new event type

Each of `adapters/{anthropic,gemini,openai-compatible}.js`'s `stream()` now yields one
additional `{type:'usage', unitKind, unitsIn, unitsOut, cachedIn, provider, model}`
event, alongside the existing `chunk`/`call`/`final`. `models/runner.js`'s per-step event
loop (already unaffected by unknown event types before this) now has one more branch,
calling `recordModelUsage()` per STEP, not once per turn — a multi-step tool-calling
turn's real total usage is captured, not just its last step.

**None of the three previously read this data — it was always sitting right there and
discarded.** `anthropic.js`'s `finalMessage()` already returns `.usage` (including
`cache_read_input_tokens`, which is what finally makes the adapter's own `cache_control`
breakpoint's real payoff measurable). `gemini.js`'s every streamed chunk carries
`usageMetadata` with CUMULATIVE totals — the last one seen is the real total, never
summed. `openai-compatible.js` needed one more change: `stream_options:
{include_usage:true}` on the request itself, since an OpenAI-shaped stream emits no
usage data at ALL without it — not merely discarded, never requested. Its final usage
chunk carries an EMPTY `choices` array, checked BEFORE the existing `if (!delta)
continue` skip, or it would silently fall through unread — the exact failure mode this
comment exists to prevent, verified against a real stub HTTP server reproducing that
exact response shape (empty `choices`, real `usage` object) before shipping.

**TTS/STT record at their own call sites, not through the adapter seam** —
`tts/index.js`'s `stream()` records character count once resolution succeeds (matches
how most providers actually bill: per character requested, not per chunk successfully
consumed by playback); `stt/deepgram.js`'s `connect()` records real connected duration
between a genuine `'open'` and `'close'` event — a rejected handshake (bad key/params,
caught by `'unexpected-response'`) never sets the start timestamp, so a failed
connection attempt is never billed time it didn't use.

## `prices.js` — three sources, write-time precedence

**Deliberately does NOT hardcode dollar figures for cloud models.** This project's model
catalog (`models/catalog.js`) names models ahead of any publicly documented, verifiable
pricing this build could honestly stand behind (`claude-opus-5`, `gemini-3.6-flash`,
`gpt-5.6-luna`, ...) — inventing a plausible-looking number for one of these would
violate the exact "never invented a number" requirement this whole subsystem exists to
satisfy. The one built-in fact this file DOES assert: a local model genuinely costs
$0 (`seedLocalModelPrices()`), never overwriting an existing row so a real user or
provider-reported price can never be silently downgraded back to a guess.
`refreshFromOpenRouter()` pulls OpenRouter's own real, public, numeric per-token pricing
(`GET /v1/models`, no key needed — the exact same data
`adapters/openai-compatible.js`'s `inferBillingFromPricing()` already parses today and
throws away) and skips any model whose price is already `source:'user'`. Both maintained
by plain periodic timers (`startPriceMaintenance()`/`startBalancePolling()` in
`balances.js`) — **deliberately NOT routed through the Heartbeat's
`registerSource()`/`routeFinding()` pipeline**, since that mechanism exists to judge
whether something is worth interrupting the owner for (`decision.js` spends a real model
call per finding, even a Tier 3 one) and a silent maintenance refresh has no finding to
judge at all.

## `advisor.js` — item 6, cost-at-decision-time

**Most of item 6 already existed** — `models/router.js`'s `scoreFor()` already weighs
`entry.tier.cost` in every scoring branch (background work is already the
cost-heaviest, at `-2x`). What was missing was that `tier.cost` was always a 1-5
name-regex GUESS (`catalog.js`'s `guessFromName()`), never a measured fact.
`observedCostTier(provider, modelId)` returns a real-price-derived value in the **exact
same 0-4 domain** the guess already used — never a new scale, never a raw dollar
adjustment — so it's a same-domain drop-in substitution `scoreFor()` was already tuned
and proven safe around, comfortably inside the `±20` `AVAILABILITY_SCORE_BONUS` spread
`router.js`'s own header comment documents as the bound nothing may ever exceed. Returns
`null` (never a guess of its own) when no real price is on record yet, and `scoreFor()`
falls back to the catalog's own `tier.cost` exactly as it always did.

## `report.js` + `check_spending.js`

`report.js` is the read side — `usageBreakdown(sinceIso)` groups by `(provider,
modelId, unitKind)`, attaches a `calculated` dollar figure only where a price is known,
and separately lists which groups had none (`pricelessGroups`) so a caller can say
plainly "X has no known price" instead of silently under-reporting. `check_spending.js`
(`core:true, meta:true`, no confirm gate — purely read-only, including its optional
`refreshBalances:true` live poll) is the entire interface for this version, per the
owner's own explicit choice: data/tool layer now, no dashboard screen until they've used
it and asked for one.

## Verified, not just read

Every arithmetic path was checked against real, hand-computed expected values, not just
"runs without throwing": a stub OpenAI-compatible HTTP server proved the adapter reads
its usage chunk correctly (`unitsIn`/`unitsOut`/`cachedIn` all exact matches); a real
`setUserPrice()` + `calculate()` round trip matched its hand-computed dollar amount
exactly (`1000*0.000003 + 500*0.000015 = 0.0105`); `report.js`'s aggregation across two
separately-recorded events summed correctly on every field. `router.js`'s integration
was confirmed non-crashing against both an empty registry and real ranking calls.
