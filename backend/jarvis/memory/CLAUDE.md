# Memory (`jarvis/memory/`)

See the root `CLAUDE.md` for the decisions that matter beyond this file (the tiered-approval
policy seam and its hard floor, fire-and-forget checkpoints, the schema-enforced
independence from conversations). This file covers the three modules.

- `store.py` — the whole Memory Manager, and the **only** module that touches `db.py` for
  Memory's tables (`memories`, `memory_versions`, `memory_categories`,
  `memory_candidates`, `memory_checkpoints`). A leaf: it imports only `db.py`. It provides:
  - CRUD with version history — `update_memory()` writes the PRE-edit state to
    `memory_versions` before overwriting. Its `origin` argument lets a caller record how the
    CURRENT text got its consent, e.g. when the user explicitly confirms a correction.
  - archive/restore, and `merge_memories()`, which folds others into a primary and archives
    them rather than deleting, so their own history stays answerable.
  - the candidate queue: `create_candidate()`, `list_pending_candidates()`,
    `approve_candidate()`, `auto_approve_candidate()`, `reject_candidate()` and
    `resolve_conflict()` (`keep-old` / `use-new` / `keep-both`; `use-new` EDITS the
    contradicted memory rather than creating a second one). Approve and auto-approve share
    one internal resolver, `_resolve_into_memory()`, so a human clicking Approve and a
    candidate clearing the trust threshold can never differ in what the memory looks like.
  - `list_pending_candidates()` hides candidates from a conversation sitting in the
    recycle bin (reversible, matching the conversation itself).
  - `conflicted_memory_ids()` — memories with a pending conflict are left out of
    `approved_memories_text()`, so the OLD claim is not injected while a human decides.
  - `approved_memories_text()` — what `prompt.py` injects, grouped by category.
  - `get_checkpoint()` / `set_checkpoint()` — the per-conversation `last_seq` pointer.
- `policy.py` — `decide(candidate, trust)`, the single seam the trust levels are built on. A
  pure function (no database) so its behaviour is an exhaustive truth table. `THRESHOLDS`:
  `ask` is `inf` (approval-first for every score, even a buggy one above 1.0), `balanced`
  is 0.85, `auto` is 0. A candidate with a `conflictWith`, or no usable `confidence`,
  ALWAYS returns `require-approval` regardless of trust — the one thing no setting overrides.
  `trust` overrides `prefs.py`'s saved `memoryTrust` for tests only.
- `review.py` — the checkpoint/extraction engine; not a leaf. One model call per
  checkpoint (`ask()`, role UTILITY), never per turn.
  - `checkpoint_conversation(id, reason)` reads only what is new since that conversation's
    own checkpoint, so a repeated checkpoint on a quiet conversation costs nothing.
  - `checkpoint_from_text(text, source_kind, source_ref)` is the non-chat path, for a
    scheduled task's result, which runs in an ephemeral session with no stored conversation.
  - Both funnel into `extract_and_file()`: the prompt hands the model the existing memories
    (for conflict detection in the SAME call) and the approved categories, and asks for
    candidates with `text`, `category`, a conflict id and `confidence`. The prompt
    calibrates confidence explicitly (0.9+ only for something stated plainly), since a
    model asked for a score anchors high otherwise.
  - Each candidate goes straight through `decide()`. Auto-approved ones are saved and
    announced with a `NOTIFICATION_CREATED` event ("Jarvis remembered something on its
    own", with a Review Memory action); the rest are announced as "something to review in
    Memory". A category not yet approved is inserted as `pending` (`propose_category()`) and
    only becomes real once something in it is saved.

**Categories are seeded once, in `db.py`'s migration step**: `Preferences`, `Projects`,
`Work`, `Learning`, `People`, `Long-term Goals`, `About You`, `Uncategorized`.
`About You` is where `remember_about_me` entries live.

**The tools** are in `jarvis/tools/memory_tools.py`: `remember_about_me`, `update_memory`,
`forget_something` (all MEDIUM risk, so all confirmed — the confirmation comes from the
risk classification, see `jarvis/tools/CLAUDE.md`), `review_memories` and
`checkpoint_memories`. `remember_about_me` and `update_memory` write `origin: 'explicit'`:
the user's confirmation of the tool's read-back already IS the approval a checkpoint exists
to obtain, so it is never queued for a second review. `update_memory` and
`forget_something` find the target by a case-insensitive substring match, and their
confirmation text reads back the ACTUAL stored text about to change.

**`jarvis/tools/search_conversations.py`** is a separate capability — not a Memory write
path, but a read-only search over `chat_store.py`'s full transcript store. See the root
`CLAUDE.md` for why the two are deliberately different mechanisms.

**`frontend/components/screens/MemoryScreen.tsx`** (`#/memory`) is the trust dial plus a
browsable, editable, searchable view over everything in `store.py`, each memory carrying its
`origin` badge. Filtering is client-side over one fetch, since Memory is a small curated
list by design.
