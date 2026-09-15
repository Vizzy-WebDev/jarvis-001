"""The SQL each schema migration runs, extracted verbatim from server/db.js.

DO NOT HAND-EDIT THE SQL BELOW. It was extracted mechanically by running the
real Node migration functions against a recording stub, so that the Python port
runs character-for-character the same DDL the Node app has been running against
the owner's live database. Re-extract rather than retype if server/db.js ever
changes (tools/record/extract-migrations.mjs).

Keyed by target user_version (1-based), matching db.js's own ordering. Three
migrations carry real logic beyond DDL and are implemented in db.py itself:
  2  — seed memory categories + one-time data/profile.json import
  6  — one-off repair of orphaned assistant tool-call rows
 10  — one-off reset of stale model availability bans in data/models.json
"""

from __future__ import annotations

MIGRATION_SQL: dict[int, list[str]] = {
    1: [
        """
      CREATE TABLE conversations (
        id         TEXT PRIMARY KEY,
        title      TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        pinned     INTEGER NOT NULL DEFAULT 0,
        archived   INTEGER NOT NULL DEFAULT 0
      );

      CREATE TABLE messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        seq             INTEGER NOT NULL,
        role            TEXT NOT NULL,
        text            TEXT,
        payload         TEXT,
        created_at      TEXT NOT NULL
      );
      CREATE INDEX idx_messages_conv ON messages(conversation_id, seq);

      CREATE VIRTUAL TABLE messages_fts USING fts5(
        text, content='messages', content_rowid='id'
      );

      CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
      END;
      CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
      END;
      CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
      END;

      CREATE TABLE app_state (
        key   TEXT PRIMARY KEY,
        value TEXT
      );
    """,
    ],
    2: [
        """
      CREATE TABLE memories (
        id         TEXT PRIMARY KEY,
        category   TEXT NOT NULL,
        text       TEXT NOT NULL,
        source_kind TEXT,
        source_ref  TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived   INTEGER NOT NULL DEFAULT 0
      );

      -- One row per PAST state of a memory, written right before an edit
      -- overwrites it — so "why did this change?" has a real answer months
      -- later, not just the current text with the old version gone.
      CREATE TABLE memory_versions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
        text       TEXT NOT NULL,
        category   TEXT NOT NULL,
        changed_at TEXT NOT NULL,
        reason     TEXT
      );
      CREATE INDEX idx_memory_versions_memory ON memory_versions(memory_id);

      -- A category's existence is itself approval-gated: a model proposing
      -- a new one inserts it here as 'pending' (rides in the same review
      -- batch as its candidates) — only the user turns it 'approved'.
      CREATE TABLE memory_categories (
        name   TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'approved'
      );

      -- A candidate is a DRAFT, not a memory — nothing here is consulted by
      -- the assistant until a human approves it into the memories table.
      CREATE TABLE memory_candidates (
        id              TEXT PRIMARY KEY,
        conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
        source_kind     TEXT NOT NULL,
        source_ref      TEXT,
        category        TEXT NOT NULL,
        text            TEXT NOT NULL,
        conflict_with   TEXT REFERENCES memories(id) ON DELETE SET NULL,
        status          TEXT NOT NULL DEFAULT 'pending',
        created_at      TEXT NOT NULL,
        resolved_at     TEXT
      );
      CREATE INDEX idx_memory_candidates_status ON memory_candidates(status);

      -- How far into each conversation extraction has already looked, so a
      -- checkpoint only ever analyses NEW messages, never re-reads the whole
      -- transcript (and never re-proposes the same candidate twice).
      CREATE TABLE memory_checkpoints (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        last_seq        INTEGER NOT NULL DEFAULT 0,
        checked_at      TEXT
      );
    """,
    ],
    3: [
        """
      ALTER TABLE memory_candidates ADD COLUMN confidence REAL;
      ALTER TABLE memories ADD COLUMN confidence REAL;
      ALTER TABLE memories ADD COLUMN origin TEXT NOT NULL DEFAULT 'approved';
    """,
        """UPDATE memories SET origin = 'legacy' WHERE source_kind = 'legacy'""",
    ],
    4: [
        """
      CREATE TABLE jobs (
        id              TEXT PRIMARY KEY,
        parent_id       TEXT REFERENCES jobs(id) ON DELETE CASCADE,
        conversation_id TEXT,
        title           TEXT NOT NULL,
        goal            TEXT NOT NULL,
        kind            TEXT NOT NULL,
        status          TEXT NOT NULL,
        plan            TEXT,
        resource        TEXT,
        recovery        TEXT,
        result          TEXT,
        error           TEXT,
        retries         INTEGER NOT NULL DEFAULT 0,
        transcript      TEXT,
        created_at      TEXT NOT NULL,
        started_at      TEXT,
        heartbeat_at    TEXT,
        finished_at     TEXT
      );
      CREATE INDEX idx_jobs_status ON jobs(status);
      CREATE INDEX idx_jobs_parent ON jobs(parent_id);

      CREATE TABLE job_trace (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id     TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        seq        INTEGER NOT NULL,
        phase      TEXT NOT NULL,
        effect     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        summary    TEXT NOT NULL,
        detail     TEXT,
        created_at TEXT NOT NULL
      );
      CREATE INDEX idx_job_trace_job ON job_trace(job_id, seq);

      CREATE TABLE job_outbox (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        tier            INTEGER NOT NULL,
        reason          TEXT NOT NULL DEFAULT 'permission',
        summary         TEXT NOT NULL,
        detail          TEXT,
        confirm_payload TEXT,
        created_at      TEXT NOT NULL,
        delivered_at    TEXT
      );
      CREATE INDEX idx_job_outbox_pending ON job_outbox(delivered_at, tier);
    """,
    ],
    5: [
        """ALTER TABLE jobs ADD COLUMN resume_note TEXT;""",
    ],
    6: [],
    7: [
        """
      CREATE TABLE improvement_outcomes (
        id          TEXT PRIMARY KEY,
        source      TEXT NOT NULL,
        source_ref  TEXT,
        entity_ref  TEXT,
        title       TEXT,
        goal        TEXT,
        kind        TEXT,
        status      TEXT NOT NULL,
        retries     INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        tool_summary TEXT,
        escalations INTEGER NOT NULL DEFAULT 0,
        reviewed_at TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE(source, source_ref)
      );
      CREATE INDEX idx_improvement_outcomes_reviewed ON improvement_outcomes(reviewed_at);
      CREATE INDEX idx_improvement_outcomes_entity ON improvement_outcomes(entity_ref);

      CREATE TABLE improvement_lessons (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL DEFAULT 'lesson',
        text        TEXT NOT NULL,
        scope       TEXT NOT NULL DEFAULT 'general',
        evidence    TEXT,
        confidence  REAL,
        source_tier INTEGER NOT NULL DEFAULT 1,
        source_url  TEXT,
        status      TEXT NOT NULL DEFAULT 'active',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
      );
      CREATE INDEX idx_improvement_lessons_status ON improvement_lessons(status);

      CREATE TABLE improvement_proposals (
        id                     TEXT PRIMARY KEY,
        kind                   TEXT NOT NULL,
        title                  TEXT NOT NULL,
        rationale              TEXT,
        helps_jarvis           TEXT,
        helps_user             TEXT,
        payload                TEXT,
        evidence               TEXT,
        source_tier            INTEGER NOT NULL DEFAULT 1,
        source_url             TEXT,
        verified               INTEGER NOT NULL DEFAULT 0,
        batch_id               TEXT,
        status                 TEXT NOT NULL DEFAULT 'pending',
        conflict_with          TEXT,
        implementation_prompt  TEXT,
        implementation_target  TEXT,
        created_at             TEXT NOT NULL,
        resolved_at            TEXT
      );
      CREATE INDEX idx_improvement_proposals_status ON improvement_proposals(status);
      CREATE INDEX idx_improvement_proposals_batch ON improvement_proposals(batch_id);

      -- The LIVE directives injected into the system prompt
      -- (improvement-store.js's activeRulesText(), prompt.js's
      -- improvementSection()). active lets the user mute a rule without
      -- unwinding its change history via undo. last_supported_at is
      -- touched whenever a later reflection cycle finds fresh evidence for
      -- the same rule, so a genuinely stale rule can eventually be told
      -- apart from one still being reinforced.
      CREATE TABLE improvement_rules (
        id                 TEXT PRIMARY KEY,
        text               TEXT NOT NULL,
        scope              TEXT NOT NULL DEFAULT 'general',
        active             INTEGER NOT NULL DEFAULT 1,
        source_proposal_id TEXT,
        last_supported_at  TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
      );
      CREATE INDEX idx_improvement_rules_active ON improvement_rules(active);

      -- The audit/undo log — append-only. Every applied change (a new rule,
      -- a pref flipped) writes ONE row here carrying both before and
      -- after; an undo writes its OWN row (kind:'undo') rather than
      -- deleting or rewriting the original, so the log never lies about
      -- what actually happened. Storing after (not just before, the
      -- way memory_versions only ever needs the PRE-edit state) is what
      -- lets undo refuse instead of clobbering: if the live value no longer
      -- matches after, the user changed it themselves since, and a blind
      -- restore would silently overwrite their own later decision.
      CREATE TABLE improvement_changes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL,
        target       TEXT NOT NULL,
        before       TEXT,
        after        TEXT,
        reason       TEXT,
        proposal_id  TEXT,
        applied_at   TEXT NOT NULL,
        undone_at    TEXT
      );
      CREATE INDEX idx_improvement_changes_target ON improvement_changes(target, applied_at);
    """,
    ],
    8: [
        """
      ALTER TABLE improvement_rules ADD COLUMN archived_at TEXT;
      CREATE INDEX idx_improvement_rules_archived ON improvement_rules(archived_at);
    """,
    ],
    9: [
        """
      CREATE TABLE self_capability_stats (
        axis          TEXT NOT NULL,
        key           TEXT NOT NULL,
        attempts      INTEGER NOT NULL DEFAULT 0,
        failures      INTEGER NOT NULL DEFAULT 0,
        last_ok_at    TEXT,
        last_failed_at TEXT,
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (axis, key)
      );

      CREATE TABLE self_goals (
        id           TEXT PRIMARY KEY,
        scope_kind   TEXT NOT NULL,
        scope_ref    TEXT NOT NULL,
        goal_text    TEXT NOT NULL,
        declared_at  TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'active',
        closed_at    TEXT
      );
      CREATE INDEX idx_self_goals_scope ON self_goals(scope_kind, scope_ref, status);
    """,
    ],
    10: [],
    11: [
        """
      CREATE TABLE capture_health (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        source        TEXT NOT NULL,
        name          TEXT,
        ok            INTEGER NOT NULL,
        error_message TEXT
      );
      CREATE INDEX idx_capture_health_ts ON capture_health(ts);
    """,
    ],
    12: [
        """
      CREATE TABLE self_model_snapshots (
        id              TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        turn_id         TEXT,
        tool_call_id    TEXT,
        snapshot_json   TEXT NOT NULL,
        created_at      TEXT NOT NULL
      );
      CREATE INDEX idx_self_model_snapshots_conv ON self_model_snapshots(conversation_id);

      CREATE TABLE self_model_citations (
        id            TEXT PRIMARY KEY,
        snapshot_id   TEXT NOT NULL REFERENCES self_model_snapshots(id) ON DELETE CASCADE,
        tool_call_id  TEXT,
        field_name    TEXT NOT NULL,
        field_value   TEXT NOT NULL,
        created_at    TEXT NOT NULL
      );
      CREATE INDEX idx_self_model_citations_snapshot ON self_model_citations(snapshot_id);
    """,
    ],
    13: [
        """
      CREATE TABLE outbox (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source          TEXT NOT NULL DEFAULT 'job',
        source_ref      TEXT,
        job_id          TEXT REFERENCES jobs(id) ON DELETE CASCADE,
        tier            INTEGER NOT NULL,
        reason          TEXT NOT NULL DEFAULT 'permission',
        summary         TEXT NOT NULL,
        detail          TEXT,
        confirm_payload TEXT,
        created_at      TEXT NOT NULL,
        delivered_at    TEXT
      );
      CREATE INDEX idx_outbox_pending ON outbox(delivered_at, tier);
      CREATE INDEX idx_outbox_source_ref ON outbox(source, source_ref);

      INSERT INTO outbox (id, source, source_ref, job_id, tier, reason, summary, detail, confirm_payload, created_at, delivered_at)
      SELECT id, 'job', job_id, job_id, tier, reason, summary, detail, confirm_payload, created_at, delivered_at FROM job_outbox;

      DROP TABLE job_outbox;

      CREATE TABLE heartbeat_schedule (
        id              TEXT PRIMARY KEY,
        source_id       TEXT NOT NULL,
        item_key        TEXT NOT NULL,
        interval_ms     INTEGER NOT NULL,
        next_due_at     TEXT NOT NULL,
        last_checked_at TEXT,
        running         INTEGER NOT NULL DEFAULT 0,
        check_state     TEXT,
        created_at      TEXT NOT NULL,
        UNIQUE(source_id, item_key)
      );
      CREATE INDEX idx_heartbeat_schedule_due ON heartbeat_schedule(next_due_at);
    """,
    ],
    14: [
        """ALTER TABLE self_goals ADD COLUMN source_turn_text TEXT;""",
    ],
    15: [
        """
      CREATE TABLE trace (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        source     TEXT NOT NULL DEFAULT 'job',
        source_ref TEXT,
        job_id     TEXT REFERENCES jobs(id) ON DELETE CASCADE,
        seq        INTEGER NOT NULL,
        phase      TEXT NOT NULL,
        effect     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        summary    TEXT NOT NULL,
        detail     TEXT,
        created_at TEXT NOT NULL
      );
      CREATE INDEX idx_trace_source_ref ON trace(source, source_ref, seq);

      INSERT INTO trace (id, source, source_ref, job_id, seq, phase, effect, kind, summary, detail, created_at)
      SELECT id, 'job', job_id, job_id, seq, phase, effect, kind, summary, detail, created_at FROM job_trace;

      DROP TABLE job_trace;
    """,
    ],
    16: [
        """
      CREATE TABLE cost_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        provider    TEXT NOT NULL,
        model_id    TEXT,
        unit_kind   TEXT NOT NULL,
        units_in    INTEGER,
        units_out   INTEGER,
        cached_in   INTEGER,
        session_id  TEXT,
        background  INTEGER NOT NULL DEFAULT 0
      );
      CREATE INDEX idx_cost_events_ts ON cost_events(ts);
      CREATE INDEX idx_cost_events_provider_model ON cost_events(provider, model_id);

      CREATE TABLE provider_balances (
        provider_ref TEXT PRIMARY KEY,
        checked_at   TEXT NOT NULL,
        detail_json  TEXT NOT NULL
      );

      CREATE TABLE model_prices (
        provider   TEXT NOT NULL,
        model_id   TEXT NOT NULL,
        unit_kind  TEXT NOT NULL,
        price_in   REAL,
        price_out  REAL,
        currency   TEXT NOT NULL DEFAULT 'USD',
        source     TEXT NOT NULL DEFAULT 'built_in',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (provider, model_id, unit_kind)
      );
    """,
    ],
    17: [
        """
      CREATE TABLE env_samples (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        cpu_pct       REAL,
        mem_free_pct  REAL,
        rss_bytes     INTEGER
      );
      CREATE INDEX idx_env_samples_ts ON env_samples(ts);
    """,
    ],
    18: [
        """
      CREATE TABLE ops_security_events (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        ts   TEXT NOT NULL
      );
      CREATE INDEX idx_ops_security_events_kind_ts ON ops_security_events(kind, ts);
    """,
    ],
    19: [
        """
      CREATE TABLE artifacts (
        id                  TEXT PRIMARY KEY,
        name                TEXT NOT NULL,
        mime_type           TEXT NOT NULL,
        size                INTEGER NOT NULL,
        session_id          TEXT,
        created_at          TEXT NOT NULL,
        verified            INTEGER,
        verification_detail TEXT
      );
      CREATE INDEX idx_artifacts_created ON artifacts(created_at);
    """,
    ],
}
