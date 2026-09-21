"""Schema added after the original 19 migrations.

Kept separate from migrations.py on purpose. That file carries a "do not hand-edit" rule so
every database built from it stays identical; hand-adding a step there would quietly break
that guarantee.

Numbering continues from 19, so `PRAGMA user_version` keeps rising monotonically.
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

    # 24: Tables for the previous AI model system (providers, models, profiles,
    # health, usage). Left as written, because a migration that has already run
    # is never edited; migration 26 drops them.
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

    # 26: Drop the previous AI model system's tables (created by 24). Nothing
    # outside that system read them. Provider and model rows are discarded;
    # credentials live in `.env` and are not touched here.
    26: [
        """
        DROP TABLE IF EXISTS ai_usage;
        DROP TABLE IF EXISTS ai_health;
        DROP TABLE IF EXISTS ai_profiles;
        DROP TABLE IF EXISTS ai_models;
        DROP TABLE IF EXISTS ai_providers;
        """
    ],

    # 27: The provider and model system that replaces the one dropped by 26 — two
    # tables and nothing else. A connection is access to one provider (keys live
    # in `.env`, named here by `secret_ref`, never stored). The models under it are
    # rows in a list: the provider's own identifier verbatim, and only what the
    # provider itself reported that a request needs (`facts_json`: an output
    # ceiling and the effort levels it accepts). No families, versions, capability
    # matrix or per-model settings. `state` is set only by a connection test;
    # `discovered_at` is when a discovery last succeeded, which is what "the
    # provider has stopped listing this" is judged against.
    27: [
        """
        CREATE TABLE IF NOT EXISTS model_providers (
          id            TEXT PRIMARY KEY,
          kind          TEXT NOT NULL,
          format        TEXT NOT NULL,
          label         TEXT NOT NULL,
          base_url      TEXT,
          secret_ref    TEXT,
          created_at    TEXT NOT NULL,
          checked_at    TEXT,
          discovered_at TEXT,
          state         TEXT NOT NULL DEFAULT 'untested',
          detail        TEXT
        );
        CREATE TABLE IF NOT EXISTS provider_models (
          provider_id  TEXT NOT NULL REFERENCES model_providers(id) ON DELETE CASCADE,
          model_id     TEXT NOT NULL,
          label        TEXT,
          source       TEXT NOT NULL,
          facts_json   TEXT,
          added_at     TEXT NOT NULL,
          last_seen_at TEXT,
          PRIMARY KEY (provider_id, model_id)
        );
        """
    ],
}
