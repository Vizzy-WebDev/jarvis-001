<!-- Ported from the Node build during the S6 cutover. The architecture, the
invariants and the live-caught bugs described here all carried over deliberately and
still hold. File paths have been updated to their real Python counterparts and are
verified to exist. Function names written in camelCase (`getToolDeclarations()`) are
the NODE originals, kept because the surrounding reasoning is about them; the Python
equivalent is the snake_case function doing that job in the same module. Where a Node
module had no Python counterpart, the text says so rather than pointing at a file that
does not exist. -->

# Memory (`jarvis/memory/*.js`)

See the root `CLAUDE.md`'s "Memory" section for the decisions that matter beyond this
file (the tiered-approval policy seam and its hard floor, fire-and-forget checkpoints,
the schema-enforced independence from conversations, why recalling a fact isn't a tool
but recalling something said is). This file covers the three modules themselves.

- `memory/store.py` — the whole Memory Manager, and the **only** module that touches
  `db.py` for Memory's tables (`memories`, `memory_versions`, `memory_categories`,
  `memory_candidates`). Leaf module (imports only `db.py`) — safe for any skill to
  import. CRUD, version history (`updateMemory()` writes the PRE-edit state to
  `memory_versions` before overwriting, never after — its optional 4th arg lets a caller
  also update the memory's `origin` badge, used when the user EXPLICITLY confirms a
  correction, since the badge should reflect how the CURRENT text got its consent, not
  just how the row was first created), archive/restore, `mergeMemories()` (folds others
  into a primary, archives them rather than deleting — their own version history stays
  answerable), and the candidate queue. `approveCandidate()` and `autoApproveCandidate()`
  both funnel through one internal `resolveCandidateIntoMemory()` — a human clicking
  Approve and a candidate clearing the trust threshold can never quietly diverge in what
  the resulting memory looks like. `approvedMemoriesText()` is what `prompt.py` injects —
  grouped by category, not a flat dump. `hydrateCandidate()` adds a conflict candidate's
  CURRENT conflicting-memory text for display (a candidate only stores the id, since the
  memory could have changed since); both `listPendingCandidates()` and
  `memory/review.py`'s broadcast payload go through it, so the review card never needs a
  second round trip to show an old-vs-new comparison.
- `memory/policy.py` — `decide(candidate, {trust})`, the single seam the trust-level
  system is built on. A pure function (no `db.py` import) on purpose — deterministic,
  testable with a plain `node -e` truth table, no server needed. `trust` overrides
  `prefs.py`'s saved `memoryTrust`, for tests; every real caller omits it. A candidate
  carrying a `conflictsWithId`, or no usable `confidence` score at all, ALWAYS returns
  `'require-approval'` regardless of trust level — the one thing no setting can override.
- `memory/review.py` — the checkpoint/extraction engine, not a leaf module (imports
  `ai.py`, `chat_store.py`, `events/bus.py`, `notifications.py`). One `askModel({json:true})`
  call per checkpoint, never per turn. `checkpointConversation(id, reason)` reads only
  what's new since that conversation's own `memory_checkpoints.last_seq` pointer
  (`chat_store.py`'s `getMessagesSince()`) — a repeated checkpoint on a quiet
  conversation costs nothing. `checkpointFromText(text, {sourceKind, sourceRef})` is
  the non-chat path (a scheduled task's own result, which runs in an ephemeral session
  that's never persisted — see `chat_store.py`'s design — so there's no stored
  conversation to read from). Both funnel into one `extractAndFile()`: the prompt hands
  the model the existing memory list (for conflict detection, in the SAME call — never
  a second one) and the approved category list, asking for
  `{candidates:[{text, category, conflictsWithId, confidence}]}` — the prompt calibrates
  `confidence` explicitly (0.9+ only for something stated plainly, most things well
  below that) since a model asked for a confidence score anchors high by default if not
  told otherwise. Each created candidate is immediately routed through `decide()`:
  `'auto-approve'` calls `autoApproveCandidate()` and the result is reported via
  `addNotification()` (bell, persisted) plus a `memory_auto_saved` broadcast so an open
  Memory screen refreshes (`frontend/lib/useHashRoute.ts`'s `refreshIfActive('memory')`);
  `'require-approval'` joins the usual `memory_candidates_ready` broadcast. A category
  not already on the approved list is inserted as `'pending'` (`proposeCategory()`)
  either way — it only becomes real the moment something in it is actually saved,
  approved or auto.

**Categories are seeded once, in `db.py`'s migration step**, not here: `Preferences`,
`Projects`, `Work`, `Learning`, `People`, `Long-term Goals`, `About You`,
`Uncategorized`. `About You` is where `remember_about_me` and the migrated
`profile.json` entries both live — see root CLAUDE.md.

**The tools** (`jarvis/tools/memory_tools.py`, `tools/memory_tools.py`,
`tools/memory_tools.py`, `tools/memory_tools.py`, `tools/memory_tools.py`) follow the same
confirm/meta anatomy every other built-in tool does (`jarvis/tools/CLAUDE.md`) — nothing
Memory-specific about that mechanism. `remember_about_me`/`update_memory` pass
`origin: 'explicit'` to `createMemory()`/`updateMemory()` — the user just directly stated
or confirmed this exact text via the tool's own confirm read-back, which already **is**
the approval a checkpoint exists to obtain elsewhere; that confirmation is never queued
for a second review. `forget_something`/`update_memory` both do a plain case-insensitive
substring match over `listMemories({})` to find what the user means (`findMatch()`,
duplicated in each file rather than shared — it's three lines and pulling it out isn't
worth a new leaf module yet), and their `summarize()` reads back the ACTUAL stored text
that will change, not just the user's vague description of it — so a confirmation always
shows what's really about to happen.

**`jarvis/tools/search_conversations.py`** is a separate capability living alongside
these — not a Memory write path, a read-only search over `chat_store.py`'s full
transcript store (every past conversation, not just approved facts). See root
`CLAUDE.md`'s Memory section for why the two are deliberately different mechanisms.

**`frontend/components/screens/MemoryScreen.tsx`** (`#/memory`) is the trust dial plus a browsable, editable,
searchable view over everything in `memory/store.py` — approved, auto-saved, or explicit
alike, each carrying its `origin` badge. Filtering is entirely client-side over one fetch
(same pattern `routes/models.py`'s toolbar already uses) since Memory is a small, curated list
by design, not something that needs server-side paging.
