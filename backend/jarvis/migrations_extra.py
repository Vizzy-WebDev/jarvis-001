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

    # 28: What actually happened when a model was called — the last time it
    # answered and the last time it failed, and why. Written after every real call
    # and read by ONE thing: Auto, which prefers models that have worked here and
    # steers around ones that just failed. It is a record of outcomes, never a
    # switch: nothing here enables, disables or hides a model, an explicit choice
    # ignores it entirely, and it is not shown as a control anywhere.
    28: [
        """
        CREATE TABLE IF NOT EXISTS model_outcomes (
          provider_id  TEXT NOT NULL REFERENCES model_providers(id) ON DELETE CASCADE,
          model_id     TEXT NOT NULL,
          last_ok_at   TEXT,
          last_fail_at TEXT,
          fail_kind    TEXT,
          fail_status  INTEGER,
          fail_message TEXT,
          PRIMARY KEY (provider_id, model_id)
        );
        """
    ],

    # 29: How quickly a model has answered (a moving average of the time to its first
    # word, or to the end of a reply that was only a tool call) and how many times in a
    # row it has failed. Both are read only by Auto: the first to prefer models that
    # respond quickly (a preference — a slow model is never excluded), the second to
    # back off a model that keeps failing without punishing one that hiccupped once.
    29: [
        """
        ALTER TABLE model_outcomes ADD COLUMN ttft_ms INTEGER;
        ALTER TABLE model_outcomes ADD COLUMN fail_streak INTEGER NOT NULL DEFAULT 0;
        """
    ],

    # 30: Specialist agents (`jarvis/agents/`). One table for every agent — the
    # built-in ones are rows seeded from `agents/builtins.py`, a custom one is a
    # row the person made — so nothing downstream can tell them apart except by
    # the `builtin` flag, which only decides whether "delete" or "reset" is
    # offered. `agent_runs` is the delegation record: who asked which agent for
    # what, and what came back — the thing to read to know what actually ran.
    # `agent_notes` is an agent's own working record (a learner's progress, what
    # has already been surfaced), deliberately apart from the user's Memory.
    30: [
        """
        CREATE TABLE IF NOT EXISTS agents (
          id                TEXT PRIMARY KEY,
          name              TEXT NOT NULL,
          description       TEXT NOT NULL DEFAULT '',
          mission           TEXT NOT NULL DEFAULT '',
          doctrine          TEXT NOT NULL DEFAULT '',
          guardrails        TEXT NOT NULL DEFAULT '',
          model_pin         TEXT,
          capability_access TEXT NOT NULL,
          memory_access     TEXT NOT NULL DEFAULT 'read',
          collaborators     TEXT NOT NULL,
          enabled           INTEGER NOT NULL DEFAULT 1,
          builtin           INTEGER NOT NULL DEFAULT 0,
          builtin_version   INTEGER,
          created_at        TEXT NOT NULL,
          updated_at        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_runs (
          id              TEXT PRIMARY KEY,
          agent_id        TEXT NOT NULL,
          parent_run_id   TEXT,
          root_run_id     TEXT,
          conversation_id TEXT,
          session_id      TEXT NOT NULL,
          requested_by    TEXT NOT NULL,
          task            TEXT NOT NULL,
          status          TEXT NOT NULL,
          result          TEXT,
          error           TEXT,
          approval_id     TEXT,
          model_id        TEXT,
          tools_used      TEXT,
          depth           INTEGER NOT NULL DEFAULT 1,
          job_id          TEXT,
          started_at      TEXT NOT NULL,
          finished_at     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_id, started_at);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, started_at);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_root ON agent_runs(root_run_id);
        CREATE TABLE IF NOT EXISTS agent_notes (
          id         TEXT PRIMARY KEY,
          agent_id   TEXT NOT NULL,
          topic      TEXT NOT NULL,
          text       TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_notes_agent ON agent_notes(agent_id, topic);
        ALTER TABLE jobs ADD COLUMN agent_id TEXT;
        """
    ],

    # 31: Content Management (`jarvis/content_manager/`) — finished content taken
    # from review to publication. An item is the publishable thing; a placement is
    # one platform/account it goes to, and after approval the item's stage is
    # DERIVED from its placements rather than set by hand. `deleted_at` is kept
    # apart from `stage` so a restore from the recycle bin puts an item back
    # exactly where it was. Revisions and events are append-only history.
    31: [
        """
        CREATE TABLE IF NOT EXISTS cm_items (
          id            TEXT PRIMARY KEY,
          name          TEXT NOT NULL,
          content_type  TEXT NOT NULL,
          niche         TEXT NOT NULL DEFAULT '',
          stage         TEXT NOT NULL,
          producer      TEXT NOT NULL DEFAULT '',
          revision      INTEGER NOT NULL DEFAULT 1,
          fields_json   TEXT NOT NULL DEFAULT '{}',
          media_json    TEXT NOT NULL DEFAULT '[]',
          findings_json TEXT NOT NULL DEFAULT '[]',
          approved_at   TEXT,
          archived_at   TEXT,
          archived_from TEXT,
          deleted_at    TEXT,
          created_at    TEXT NOT NULL,
          updated_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cm_items_stage ON cm_items(deleted_at, stage);
        CREATE TABLE IF NOT EXISTS cm_placements (
          id             TEXT PRIMARY KEY,
          item_id        TEXT NOT NULL REFERENCES cm_items(id) ON DELETE CASCADE,
          platform       TEXT NOT NULL,
          account_id     TEXT,
          account_label  TEXT NOT NULL DEFAULT '',
          destination    TEXT NOT NULL DEFAULT '',
          overrides_json TEXT NOT NULL DEFAULT '{}',
          status         TEXT NOT NULL DEFAULT 'draft',
          scheduled_at   TEXT,
          timezone       TEXT,
          claimed_by     TEXT,
          claimed_at     TEXT,
          published_at   TEXT,
          published_url  TEXT,
          failure        TEXT,
          metrics_json   TEXT,
          created_at     TEXT NOT NULL,
          updated_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cm_placements_item ON cm_placements(item_id);
        CREATE INDEX IF NOT EXISTS idx_cm_placements_due ON cm_placements(status, scheduled_at);
        CREATE TABLE IF NOT EXISTS cm_change_requests (
          id                TEXT PRIMARY KEY,
          item_id           TEXT NOT NULL REFERENCES cm_items(id) ON DELETE CASCADE,
          revision          INTEGER NOT NULL,
          what              TEXT NOT NULL,
          why               TEXT NOT NULL DEFAULT '',
          assignee          TEXT NOT NULL,
          status            TEXT NOT NULL DEFAULT 'open',
          picked_up_by      TEXT,
          picked_up_at      TEXT,
          job_id            TEXT,
          start_error       TEXT,
          created_at        TEXT NOT NULL,
          resolved_at       TEXT,
          resolved_revision INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_cm_requests_item ON cm_change_requests(item_id);
        CREATE INDEX IF NOT EXISTS idx_cm_requests_status ON cm_change_requests(status);
        CREATE TABLE IF NOT EXISTS cm_revisions (
          item_id     TEXT NOT NULL REFERENCES cm_items(id) ON DELETE CASCADE,
          revision    INTEGER NOT NULL,
          fields_json TEXT NOT NULL,
          media_json  TEXT NOT NULL,
          by          TEXT NOT NULL DEFAULT '',
          note        TEXT NOT NULL DEFAULT '',
          created_at  TEXT NOT NULL,
          PRIMARY KEY (item_id, revision)
        );
        CREATE TABLE IF NOT EXISTS cm_events (
          id       INTEGER PRIMARY KEY AUTOINCREMENT,
          item_id  TEXT NOT NULL REFERENCES cm_items(id) ON DELETE CASCADE,
          at       TEXT NOT NULL,
          actor    TEXT NOT NULL,
          kind     TEXT NOT NULL,
          note     TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_cm_events_item ON cm_events(item_id, id);
        CREATE TABLE IF NOT EXISTS cm_accounts (
          id                TEXT PRIMARY KEY,
          platform          TEXT NOT NULL,
          handle            TEXT NOT NULL,
          destinations_json TEXT NOT NULL DEFAULT '[]',
          default_niche     TEXT NOT NULL DEFAULT '',
          created_at        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cm_files (
          id         TEXT PRIMARY KEY,
          item_id    TEXT REFERENCES cm_items(id) ON DELETE CASCADE,
          name       TEXT NOT NULL,
          mime       TEXT NOT NULL,
          size       INTEGER NOT NULL,
          suffix     TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cm_files_item ON cm_files(item_id);
        """
    ],
    # 32: Content Management keeps content and its workflow only. The accounts list
    # (and a placement's account) is removed: which account a post goes out on is
    # the publishing tool's business, not something this screen manages. A
    # placement may carry its OWN media (a vertical cut, a different thumbnail),
    # falling back per role to the item's. Reported numbers are dated snapshots,
    # one row each, so day 1 and day 30 both survive; the latest is also kept on
    # the placement (`metrics_json`) for cheap reads.
    32: [
        """
        DROP TABLE IF EXISTS cm_accounts;
        ALTER TABLE cm_placements DROP COLUMN account_id;
        ALTER TABLE cm_placements DROP COLUMN account_label;
        ALTER TABLE cm_placements ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]';
        CREATE INDEX IF NOT EXISTS idx_cm_placements_item_status ON cm_placements(item_id, status);
        CREATE TABLE IF NOT EXISTS cm_metrics (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          placement_id TEXT NOT NULL REFERENCES cm_placements(id) ON DELETE CASCADE,
          item_id      TEXT NOT NULL REFERENCES cm_items(id) ON DELETE CASCADE,
          captured_at  TEXT NOT NULL,
          reported_at  TEXT NOT NULL,
          source       TEXT NOT NULL DEFAULT '',
          metrics_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cm_metrics_placement ON cm_metrics(placement_id, captured_at);
        """
    ],
    # 33: A niche is a folder the person opens, not only a label: it can exist
    # while empty and be renamed. The list is its own small table; an item still
    # carries its niche's NAME (`cm_items.niche`), so every filter, search and
    # count that already reads it keeps working. Existing labels become folders,
    # and two spellings of one niche ("psychology", "Psychology") become one —
    # the spelling most items use wins.
    33: [
        """
        CREATE TABLE IF NOT EXISTS cm_niches (
          name       TEXT PRIMARY KEY COLLATE NOCASE,
          created_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO cm_niches (name, created_at)
          SELECT niche, MIN(created_at) FROM cm_items WHERE niche != ''
          GROUP BY niche ORDER BY COUNT(*) DESC, niche;
        UPDATE cm_items SET niche = (SELECT n.name FROM cm_niches n WHERE n.name = cm_items.niche)
          WHERE niche != '';
        CREATE INDEX IF NOT EXISTS idx_cm_items_niche ON cm_items(niche COLLATE NOCASE);
        """
    ],
}
