"""Schema this build adds beyond the 19 migrations inherited from the Node app.

Kept separate from migrations.py on purpose. That file was extracted mechanically
from server/db.js and carries a "do not hand-edit" rule so it stays a faithful
copy; hand-adding a step there would quietly break that guarantee. These are ours.

Numbering continues from 19, so `PRAGMA user_version` keeps rising monotonically.
Before the Node app was retired, this was also what kept it safe to open the same
database file: its own migrate() loop ran `for v = current; v < MIGRATIONS.length`
with 19 entries, so a database already at version 20+ simply did no work there.
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

    # 23: A job says how it is going, and how much it matters (§13).
    #
    # "A job model with: id, type, status, priority, progress, timestamps,
    # result, error." The table had every one of those except priority and any
    # notion of progress — so "how far along is it?" could only be answered by
    # reading a trace, and "which of these three matters most?" could not be
    # answered at all.
    #
    # `progress` is 0-100 and NULL until the work itself reports one: a job that
    # cannot say how far along it is must not be shown as 0%, which reads as
    # "stuck", nor as an invented fraction. `current_step` is what it is doing
    # right now, in the user's words, so "checking on that" has a real answer.
    23: [
        """
        ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 2;
        ALTER TABLE jobs ADD COLUMN progress INTEGER;
        ALTER TABLE jobs ADD COLUMN current_step TEXT;
        CREATE INDEX IF NOT EXISTS idx_jobs_priority ON jobs(priority, created_at);
        """
    ],

    # 24: The AI Model System — providers, models, profiles, health, usage.
    #
    # A ground-up rebuild of the model infrastructure layer (jarvis/ai/),
    # replacing the JSON-file-backed connections/deployments/availability/effort
    # stores the earlier catalog/gateway build used. Five entities, each with a
    # real reason to be its own table rather than folded into another:
    #
    # `ai_providers` is a saved address + credential reference + wire format.
    # `ai_models` is one model made callable through one provider — the
    # crossing-axis unit a router actually chooses between, so the same model
    # reachable two ways is two independent rows with their own health and
    # usage, never one row silently shared. `capabilities_discovered_json`
    # (what the provider reported) and `capabilities_override_json` (what a
    # person corrected) are kept as separate columns rather than one merged
    # blob so provenance survives a restart; the third source — this build's
    # own small seed of known model families — is a pattern table matched
    # fresh on every read (`ai/registry.py`'s `SEED`), never stored, so a
    # better seed shipped tomorrow applies to an existing row with no
    # migration touching it. Same shape for `reasoning_*_json`.
    #
    # `ai_health` is its own table, not a column on `ai_models`, because it is
    # written on nearly every call and read on nearly every routing decision —
    # a write-heavy, small-payload access pattern that has no business sharing
    # a row (and a lock) with a model's rarely-changed configuration.
    #
    # `ai_usage` is the per-request ledger (§17/§30): one row per attempted
    # call, success or failure, carrying enough to reconstruct why a model was
    # chosen and what happened when it answered — `fallback_chain_json` is the
    # ordered list of every candidate tried before this row's outcome.
    24: [
        """
        CREATE TABLE IF NOT EXISTS ai_providers (
          id                   TEXT PRIMARY KEY,
          label                TEXT NOT NULL,
          kind                 TEXT NOT NULL,
          adapter              TEXT NOT NULL,
          base_url             TEXT,
          auth_method          TEXT NOT NULL DEFAULT 'api_key',
          credential_ref       TEXT,
          key_required         INTEGER,
          enabled              INTEGER NOT NULL DEFAULT 1,
          builtin              INTEGER NOT NULL DEFAULT 0,
          discovery_supported  INTEGER,
          config_json          TEXT NOT NULL DEFAULT '{}',
          created_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_models (
          id                            TEXT PRIMARY KEY,
          provider_id                   TEXT NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
          native_model_id               TEXT NOT NULL,
          display_name                  TEXT,
          label                         TEXT,
          family                        TEXT,
          version_label                 TEXT,
          status                        TEXT NOT NULL DEFAULT 'unknown',
          release_date                  TEXT,
          deprecation_date              TEXT,
          context_window                INTEGER,
          max_input_tokens              INTEGER,
          max_output_tokens             INTEGER,
          capabilities_discovered_json  TEXT NOT NULL DEFAULT '{}',
          capabilities_override_json    TEXT NOT NULL DEFAULT '{}',
          parameters_json               TEXT NOT NULL DEFAULT '{}',
          reasoning_discovered_json     TEXT NOT NULL DEFAULT '{}',
          reasoning_override_json       TEXT NOT NULL DEFAULT '{}',
          pricing_json                  TEXT,
          quality                       INTEGER,
          enabled                       INTEGER NOT NULL DEFAULT 1,
          notes                         TEXT,
          discovered_at                 TEXT,
          created_at                    TEXT NOT NULL,
          UNIQUE(provider_id, native_model_id)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_models_provider ON ai_models(provider_id);

        CREATE TABLE IF NOT EXISTS ai_profiles (
          id                  TEXT PRIMARY KEY,
          label               TEXT NOT NULL,
          model_id            TEXT REFERENCES ai_models(id) ON DELETE SET NULL,
          reasoning_level     TEXT,
          params_json         TEXT NOT NULL DEFAULT '{}',
          tool_behavior_json  TEXT NOT NULL DEFAULT '{}',
          is_default          INTEGER NOT NULL DEFAULT 0,
          builtin             INTEGER NOT NULL DEFAULT 0,
          created_at          TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_health (
          model_id      TEXT PRIMARY KEY REFERENCES ai_models(id) ON DELETE CASCADE,
          state         TEXT NOT NULL,
          detail        TEXT,
          technical     TEXT,
          since         TEXT NOT NULL,
          failure_count INTEGER NOT NULL DEFAULT 0,
          last_success  TEXT,
          last_failure  TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_usage (
          id                   INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id           TEXT NOT NULL,
          ts                   TEXT NOT NULL,
          provider_id          TEXT,
          model_id             TEXT,
          role                 TEXT,
          tokens_in            INTEGER,
          tokens_out           INTEGER,
          tokens_reasoning     INTEGER,
          cached_in            INTEGER,
          cost_estimate        REAL,
          latency_ms           INTEGER,
          ttft_ms              INTEGER,
          success              INTEGER NOT NULL,
          error_type           TEXT,
          fallback_chain_json  TEXT,
          session_id           TEXT,
          background           INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON ai_usage(ts);
        CREATE INDEX IF NOT EXISTS idx_ai_usage_model ON ai_usage(model_id);
        CREATE INDEX IF NOT EXISTS idx_ai_usage_request ON ai_usage(request_id);
        """
    ],

    # 25: A recycle bin for chat history, the same shape the notifications
    # store already uses for its own trash — deleting a conversation moves it
    # here instead of dropping it (and, via ON DELETE CASCADE, every message
    # in it) immediately and irreversibly. NULL means "not trashed", so every
    # existing conversation is unaffected by this migration; a real timestamp
    # is both the recycle-bin sort key and the 30-day auto-purge clock.
    25: [
        """
        ALTER TABLE conversations ADD COLUMN deleted_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_conversations_deleted ON conversations(deleted_at);
        """
    ],
}
