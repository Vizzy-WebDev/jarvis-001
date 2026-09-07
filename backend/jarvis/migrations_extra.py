"""Schema this build adds beyond the 19 migrations inherited from the Node app.

Kept separate from migrations.py on purpose. That file was extracted mechanically
from server/db.js and carries a "do not hand-edit" rule so it stays a faithful
copy; hand-adding a step there would quietly break that guarantee. These are ours.

Numbering continues from 19, so `PRAGMA user_version` keeps rising monotonically.
The Node app is unaffected: its own migrate() loop runs `for v = current; v <
MIGRATIONS.length` with 19 entries, so a database at version 20+ simply does no
work there. Additive, and safe for both to open the same file.
"""

from __future__ import annotations

EXTRA_MIGRATION_SQL: dict[int, list[str]] = {
    # 20: Approvals (§8, §40) and standing permission grants.
    #
    # The current implementation keeps pending confirmations in a module-level
    # Map with a TTL checked only on redemption — so a token minted and never
    # answered retains its captured arguments for the life of the process, and
    # every pending approval is lost on restart. §40 lists `approvals` as a real
    # entity and TEST 12 expects state to survive a restart per its persistence
    # policy; an unanswered question about deleting a folder should still be
    # there when the app comes back.
    20: [
        """
        CREATE TABLE IF NOT EXISTS approvals (
          id            TEXT PRIMARY KEY,
          operation_id  TEXT NOT NULL,
          capability    TEXT NOT NULL,
          args          TEXT,
          -- Argument names to hide whenever this row is displayed or published.
          redact_args   TEXT,
          risk          TEXT NOT NULL,
          session_id    TEXT NOT NULL,
          -- The turn that ASKED. An approval may never be resolved and executed
          -- inside the same turn that requested it: that is the model minting a
          -- question and answering it itself, which was reproduced live on the
          -- voice path.
          turn_id       TEXT NOT NULL,
          surface       TEXT NOT NULL,
          reason        TEXT,
          status        TEXT NOT NULL DEFAULT 'pending',
          requested_at  TEXT NOT NULL,
          resolved_at   TEXT,
          expires_at    TEXT,
          resolved_by   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
        CREATE INDEX IF NOT EXISTS idx_approvals_session ON approvals(session_id);
        -- §49: one operation, one approval. A request delivered twice finds the
        -- existing row instead of asking the user the same question again.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_operation
          ON approvals(operation_id);

        CREATE TABLE IF NOT EXISTS permission_grants (
          id          TEXT PRIMARY KEY,
          -- An exact capability name, or '*'. A wildcard never covers HIGH risk;
          -- that floor lives in the policy, not in the data.
          capability  TEXT NOT NULL,
          session_id  TEXT,
          granted_at  TEXT NOT NULL,
          expires_at  TEXT,
          revoked_at  TEXT,
          note        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_grants_capability ON permission_grants(capability);
        """
    ],

    # 21: Completed operations (§49).
    #
    # "If a request is accidentally processed twice — 'Create reminder for 9 AM' —
    # do not create two reminders simply because of a retry." An operation id is
    # recorded the moment a capability finishes, so a second delivery returns the
    # first result instead of running the side effect again. Persisted rather
    # than in-memory so a duplicate that arrives after a restart is still caught.
    21: [
        """
        CREATE TABLE IF NOT EXISTS operations (
          operation_id TEXT PRIMARY KEY,
          capability   TEXT NOT NULL,
          session_id   TEXT,
          outcome      TEXT NOT NULL,
          ok           INTEGER NOT NULL,
          result       TEXT,
          error        TEXT,
          attempts     INTEGER NOT NULL DEFAULT 1,
          duration_ms  INTEGER,
          completed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_operations_session ON operations(session_id);
        """
    ],

    # 22: Memory gets importance and an expiry (§22).
    #
    # "Memory entries should have: content, category, importance, source,
    # timestamps, and optionally expiry." The store had category, content,
    # source, confidence, origin and timestamps; these are the two the
    # directive names that were missing.
    #
    # `importance` is 1-5 and NULL until something actually judges it — a
    # default of 3 would be a number nobody chose, presented later as if
    # someone had. `expires_at` is NULL for the ordinary case: most things
    # worth remembering do not expire, and a memory with an expiry is making a
    # specific claim ("they are in Lisbon until the 14th") that should stop
    # being asserted once it lapses, rather than quietly ageing into a lie.
    22: [
        """
        ALTER TABLE memories ADD COLUMN importance INTEGER;
        ALTER TABLE memories ADD COLUMN expires_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_memories_expires ON memories(expires_at);
        """
    ],
}
