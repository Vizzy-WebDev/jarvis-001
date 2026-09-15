# The gateway (`jarvis/gateway/`)

**The one place a model call goes through.** In the Node implementation there was no
single gateway: `adapter.stream()` was called from three places, each re-implementing
candidate selection, failover, health marking and availability recording, and two more
callers bypassed the adapter layer entirely. One of the three never marked health at all,
so a model that failed during computer control was never benched. This package exists so
that logic lives once.

Two axes cross here and neither contains the other:

* the **catalog** (`jarvis/catalog/`) says what a model IS — provider, family, version,
  capabilities, reasoning scheme
* a **connection** (`connections.py`) says how to REACH one — address, credential, wire
  format

They meet at a **deployment** (`deployments.py`): one model version reached through one
connection, and the thing the router actually chooses between.

## Why the deployment is the unit

The flat model row it replaced had a problem that was not about its fields but about its
identity: a row WAS a model, so a model reachable two ways had to be two unrelated rows
that nothing connected, and a model reachable one way had nowhere to record which of its
facts came from the provider and which from a regex over its name.

A deployment owns what belongs to the pairing rather than to either side: whether the user
switched it on, what it has cost through THIS connection, how quickly it answers through
it, and whether it is currently in cooldown.

**Availability and latency stay keyed on the deployment, and that is not an
implementation detail.** It would read more naturally to bench a version — the model is
what failed, after all. It would also mean one rate-limited gateway route taking the same
model offline when reached with your own key, which is exactly what the crossing axis
exists to make impossible. `tests/test_deployments.py` asserts it directly, because the
version-keyed version of that code passes every other test in the file.

**Refused-parameter facts are keyed per version instead** (`effort.py`), because "this
model does not accept that argument" is a fact about the model, not about the route.

## The files

- **`deployments.py`** — the routable unit. Hydrates the connection's facts and the
  catalog's answer at READ time, so neither can drift: a stored copy of an address goes
  stale the moment the connection is edited, and a stored copy of what a model can do
  cannot pick up a capability the catalog learned afterwards. `RESERVED_IDS` refuses ids
  that would collide with a static route segment — a model called "Catalog" would
  otherwise be unreachable through `/api/models/<id>` with nothing reporting a problem.
- **`routing.py`** — the ONE ranking function, and also where `Role` lives. `Role` is a
  per-request classification a caller tags its own `Task` with (conversation, voice,
  control, background, utility) — `_score()` weighs the ranking by it, the same way it
  weighs by the balance dial. A persisted, user-configurable per-role model/effort pin
  used to live here too (`slots.py`, plus `routes/roles.py`'s API and a settings screen)
  and was removed: that was an application-level preference bolted into the gateway,
  not something the ranking function itself needed to own. `build_candidates()` is used
  by every caller rather than re-derived per call site, and `need` is enforced here
  rather than by each caller. `explain_exclusions()` walks the SAME predicate, so a
  "nothing can answer this" message can never name a different reason than the one that
  actually excluded.
- **`effort.py`** — resolving "think this hard" against what a specific version accepts,
  and remembering what a version has refused. Clamping goes DOWN, never up, and is
  reported. `call_with_effort()` keeps retry policy on this side, out of the three wire
  formats that would otherwise drift.
- **`latency.py`** — time to FIRST TOKEN per deployment, which is what replaced the
  authored `tier.speed` the catalog deleted. Not total duration: that mostly measures how
  long the reply was, so ranking on it would learn the shape of recent questions rather
  than anything about the model.
- **`availability.py`** — whether a deployment is usable right now. One store, one
  vocabulary, its own file. In the Node version, recording availability went through
  `updateModel()` — read-whole-file/modify/write-whole-file with no locking — and "Check
  all models" ran three of those concurrently, so results were silently lost on exactly
  the screen that exists to show them.
- **`discovery.py`** — asking a provider what it has, and reconciling that with what is
  configured, including noticing a model that stopped being listed. `normalise()` produces
  what gets STORED; `for_picker()` produces what the add-a-model screen shows. Both the
  discovery route and the custom-address probe use the second one, so a model cannot be
  grouped differently depending on which route the user came in by.
- **`client.py`** — `Gateway`, the `ModelClient` the orchestrator talks to. Builds the
  candidate list, decides how hard to think, marks every failure, and says what went
  wrong in plain language when nothing can serve the turn.
- **`connections.py` / `providers.py` / `probe.py` / `setup.py`** — saving an address and
  key, the five setup presets, working out the wire format of an address nobody declared,
  and the one call that creates a connection with its first models.
- **`error_kind.py`** — the only kind→state mapping. **`jsonish.py`** — pulling JSON out
  of a model's prose.

## Things that will be tempting and are wrong

**Do not let the gateway hold a routing setting.** `Gateway` is constructed with no
`balance`: it used to be read once when the orchestrator singleton was built, so changing
the Fast/Balanced/Quality dial did nothing until the process restarted. The router reads
it per turn.

**Do not collapse `UNKNOWN` into `NO` in the candidate filter.** Only a definite `NO`
excludes. The exception is named once, per capability, in `MUST_BE_CERTAIN`: web search is
the one whose absence fails SILENTLY — a model that cannot search answers from memory,
fluently, citing nothing — so unproven is excluded there and nowhere else.

**Do not derive "who makes this model" a second time.** `deployments.provider_of()` is the
one answer, and three sides depend on agreeing: the observer that records spend, the router
that reads a price back, and the seeder that writes a starting price. They did not agree
before the switchover — the seeder filed under the adapter name while the observer recorded
under the connection's provider — so every `$0` row seeded for a local model was read back
by nothing.

**Do not reintroduce a persisted per-role model/effort pin inside this package.** One
existed (`slots.py`) and was deliberately removed: every deployment a pin can reach is a
real, callable, billable candidate, and deciding "which model answers which kind of
request" is a policy choice an application makes, not a fact the ranking function should
store. If a caller wants a specific deployment, `Task`/`model_id` already carry that per
request — `role` on `Task` is the classification a caller supplies; it was never meant to
also be where a stored preference lives.
