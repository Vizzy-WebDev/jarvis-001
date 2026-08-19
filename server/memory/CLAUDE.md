# Memory (`server/memory/*.js`)

See the root `CLAUDE.md`'s "Memory" section for the decisions that matter beyond this
file (the approval-first policy seam, fire-and-forget checkpoints, the schema-enforced
independence from conversations, why "recall" isn't a tool). This file covers the three
modules themselves.

- `memory-store.js` — the whole Memory Manager, and the **only** module that touches
  `db.js` for Memory's tables (`memories`, `memory_versions`, `memory_categories`,
  `memory_candidates`). Leaf module (imports only `db.js`) — safe for any skill to
  import. CRUD, version history (`updateMemory()` writes the PRE-edit state to
  `memory_versions` before overwriting, never after), archive/restore, `mergeMemories()`
  (folds others into a primary, archives them rather than deleting — their own version
  history stays answerable), and the candidate queue
  (`create/approve/reject/resolveConflict Candidate`). `approvedMemoriesText()` is what
  `prompt.js` injects — grouped by category, not a flat dump. `hydrateCandidate()` adds
  a conflict candidate's CURRENT conflicting-memory text for display (a candidate only
  stores the id, since the memory could have changed since); both `listPendingCandidates()`
  and `memory-review.js`'s broadcast payload go through it, so the review card never
  needs a second round trip to show an old-vs-new comparison.
- `memory-policy.js` — `decide(candidate)`, the single seam a future trust-level system
  replaces. Always returns `'require-approval'` in this build. Every write path asks
  this function; none hardcode "ask the user" themselves — see root CLAUDE.md for why
  that split exists.
- `memory-review.js` — the checkpoint/extraction engine, not a leaf module (imports
  `ai.js`, `chat-store.js`, `events.js`). One `askModel({json:true})` call per
  checkpoint, never per turn. `checkpointConversation(id, reason)` reads only what's
  new since that conversation's own `memory_checkpoints.last_seq` pointer
  (`chat-store.js`'s `getMessagesSince()`) — a repeated checkpoint on a quiet
  conversation costs nothing. `checkpointFromText(text, {sourceKind, sourceRef})` is
  the non-chat path (a scheduled task's own result, which runs in an ephemeral session
  that's never persisted — see `chat-store.js`'s design — so there's no stored
  conversation to read from). Both funnel into one `extractAndFile()`: the prompt hands
  the model the existing memory list (for conflict detection, in the SAME call — never
  a second one) and the approved category list, asking for
  `{candidates:[{text, category, conflictsWithId}]}`. A category not already on the
  approved list is inserted as `'pending'` (`proposeCategory()`) — it only becomes real
  the moment something in it is actually approved. On any candidates found, broadcasts
  `memory_candidates_ready` over SSE — the review card in `public/app.js` appears on
  its own; nothing polls for it.

**Categories are seeded once, in `db.js`'s migration step**, not here: `Preferences`,
`Projects`, `Work`, `Learning`, `People`, `Long-term Goals`, `About You`,
`Uncategorized`. `About You` is where `remember_about_me` and the migrated
`profile.json` entries both live — see root CLAUDE.md.

**The skills** (`server/skills/remember_about_me.js`, `forget_something.js`,
`update_memory.js`, `review_memories.js`, `checkpoint_memories.js`) follow the same
confirm/meta anatomy every other skill does (`server/skills/CLAUDE.md`) — nothing
Memory-specific about that mechanism. `forget_something`/`update_memory` both do a
plain case-insensitive substring match over `listMemories({})` to find what the user
means (`findMatch()`, duplicated in each file rather than shared — it's three lines and
pulling it out isn't worth a new leaf module yet), and their `summarize()` reads back
the ACTUAL stored text that will change, not just the user's vague description of it —
so a confirmation always shows what's really about to happen.
