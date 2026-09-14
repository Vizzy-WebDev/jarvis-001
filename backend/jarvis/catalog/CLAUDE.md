# The catalog (`jarvis/catalog/`)

**What a model IS.** Provider, family, version, what it can do, what reasoning control it
offers. It owns nothing, calls nothing, and imports nothing that knows how to reach a
model — no gateway, no adapters, no store, no database. `tests/test_architecture.py`
asserts that rather than trusting it: the pull the other way is constant, because the
obvious place to put "and here is how to reach it" is beside "and here is what it can do",
and an edge from here to the gateway is a real cycle (the gateway imports this, and so do
the adapters — which is why it lives here rather than under `gateway/`).

Before this package, a model was a name string and everything else about it was inferred
by matching regular expressions against that string. That is why an unbounded `mini`
matched inside "ge**mini**" and scored every Gemini model, Pro included, as a cheap fast
one.

## The three decisions that carry the weight

**Capability support has THREE states.** `Support.YES`, `NO` and `UNKNOWN` are different
answers and none collapses into another. "Nobody has established whether this model can
see an image" is not "it cannot" — answering it as `NO` hides a capable model with no
visible reason, and answering it `YES` sends an image to a model that will fail on it. For
most models on most capabilities, `UNKNOWN` is the honest answer, and the rest of the
system is built to route it (see `gateway/routing.py`'s `MUST_BE_CERTAIN` for the one
capability where unknown is *not* good enough).

**Effort is declared by a version, never inferred.** A version says which `EffortKind` it
speaks — `TIERS`, `BUDGET`, `VARIANT`, `NONE` or `UNKNOWN` — and which rungs of the ladder
it offers. `NONE` and `UNKNOWN` are distinct for the same reason as above: a model with no
reasoning control is not a model nobody has asked about.

**A family is deliberately thin.** It groups versions and gives a lineage something to be
matched against; it carries no capabilities and no limits. The facts that matter vary
WITHIN a family — one release sees images, the next adds video — so putting them on the
family would mean two places to look for the truth and a version quietly reporting its
family's stale answer.

## `known.py` — the small amount Jarvis ships knowing

**Keyed by PATTERN, not by exact model id.** The table this replaced did
`KNOWN.get(model)` against bare names like `"claude-haiku-4-5"`, while provider listings
return dated snapshots (`claude-haiku-4-5-20251001`). One character of difference dropped
a model straight through to name guessing. That is not a table anyone can keep up to date;
it is a table that is wrong by construction, because the ids it is keyed on are not the
ids that arrive.

**`SEED` may be empty, and the architecture survives that.** `tests/test_catalog.py` runs
the whole resolution path with `rules=()` and asserts a usable version comes out —
everything unknown, nothing invented, still callable. That test is the real specification
of this file; the entries are a convenience on top of it. A catalog that only works when
it has an entry for your model is a hardcoded model list wearing a hat.

**It carries only what the pipeline needs and cannot get another way.** Price and speed
failed that test and are gone rather than ported: Jarvis records real spend
(`cost/advisor.py`) and real latency (`gateway/latency.py`), and a measured number beats an
authored one. What is left is the reasoning scheme, which no first-party API exposes, and
the family, which no API exposes either. `quality` survives as the only authored number,
because there is no measurable proxy for it short of an evaluation harness this build is
not growing.

A rule's `provider` is the lineage's OWNER, which is not necessarily the connection the
model is reached through. A gateway reselling someone else's model is still serving that
maker's model; saying otherwise would make the catalog describe the plumbing.

## `merge.py` — three sources, one precedence

`resolve()` answers everything known about one callable id, per field, with its source
attached: `USER > DISCOVERED > CATALOG > DEFAULT`. Provenance is recorded **per field**
because a version is almost always a mixture — its id discovered, its family matched from
a pattern, its context window whatever the provider volunteered — and a single flag on the
whole object cannot say which parts to trust. Capabilities merge **per flag**, so a
provider that reports only vision cannot wipe a known-good tools answer beside it.

**Nothing here trusts its input.** `discovered` and `user` are read straight off a stored
deployment, and `overrides` is reachable from a PATCH body. Resolution runs at READ time on
every routing pass, so a value that raises does not spoil one request — it empties the
roster, and with it the models screen, the status route and every turn. A malformed field
degrades to "we do not know"; a `quality` outside 0-5 is refused rather than clamped,
because the scoring formula was never tuned against it and a large enough number would
outrank `routing.AVAILABILITY_BONUS` and put a dead model first.
