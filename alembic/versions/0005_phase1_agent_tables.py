"""0005 — Phase-1 agent-ecosystem tables

Revision ID: 0005
Revises: 0004
Create Date: Phase-1 (Gate G1 -> G2)

Adds the five new schema tables Phase-1 scope calls for
(AETHER_PHASE1_PROMPT §2 / §4, Implementation Plan §21 DDL, Missing
Specs Volume 3 DDL):

    sub_agents        — registry/catalog of the 30 sub-agents
    sub_agent_runs    — one row per sub-agent invocation (append-in-spirit)
    skill_chains      — the named production chains (Impl Plan §8.4)
    skill_performance — rolling accuracy/latency/error windows per skill
    meta_loop_runs    — Meta-Loop health scorecards

Reconciliation note (governance precedence applied):
The Implementation Plan §21 DDL and Missing Specs Volume 3 DDL describe
the SAME five tables. The Implementation Plan version is an abridged
column list; the Missing Specs version is a strict superset that adds
CHECK constraints, indexes, and the ``description``/``updated_at``
columns the abridged form omits. They do not conflict, so — per
"Implementation Plan governs within its scope > supporting docs" with
no actual conflict to resolve — this migration builds the fuller
Missing Specs form, which satisfies both.

Enum note:
``sub_agent_status`` (and every other enum used here) was already
created by migration 0001 — ``aether.models.enums.PG_ENUM_TYPE_NAMES``
lists all 19 types and 0001 creates them all up front, even the ones
no Phase-0 table referenced. This migration therefore only *references*
``sub_agent_status`` and ``skill_status``; it never CREATE TYPEs them.

Privilege pattern (same as Phase-0, HANDOFF.md):
The runtime role ``aether_app_role`` is granted SELECT/INSERT/UPDATE on
all five tables and never DELETE (INV-02 in spirit: nothing the app
touches is ever hard-deleted; append-only run/perf history is removed
only by out-of-band retention archival, not by the application). An
explicit REVOKE DELETE is emitted on the three append-only tables as
defense-in-depth, mirroring how 0004 revoked DELETE on
skill_invocation_log.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "aether_app_role"


def upgrade() -> None:
    # ---- sub_agents --------------------------------------------------
    # Registry of the 30 sub-agents. Mutable catalog (status may move
    # active -> deprecated -> inactive), so UPDATE is granted; no
    # updated_at column is specified by the schema, so no trigger.
    op.execute(
        sa.text(
            """
            CREATE TABLE sub_agents (
                id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                name                    TEXT        NOT NULL UNIQUE,
                parent_agent            TEXT        NOT NULL,
                domain                  TEXT        NOT NULL,
                description             TEXT,
                trigger_event           TEXT        NOT NULL,
                termination_condition   TEXT        NOT NULL,
                max_duration_ms         INTEGER     NOT NULL DEFAULT 60000
                                                    CHECK (max_duration_ms > 0),
                max_iterations          INTEGER     NOT NULL DEFAULT 1
                                                    CHECK (max_iterations > 0),
                authority_level         INTEGER     NOT NULL DEFAULT 0
                                                    CHECK (authority_level BETWEEN 0 AND 5),
                phase_introduced        INTEGER     NOT NULL DEFAULT 1
                                                    CHECK (phase_introduced BETWEEN 0 AND 3),
                status                  TEXT        NOT NULL DEFAULT 'active'
                                                    CHECK (status IN ('active','deprecated','inactive')),
                created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_sub_agents_parent ON sub_agents(parent_agent)"))
    op.execute(sa.text("CREATE INDEX idx_sub_agents_domain ON sub_agents(domain)"))

    # ---- sub_agent_runs ---------------------------------------------
    # One row per sub-agent invocation. Inserted at spawn, UPDATEd to a
    # terminal status/terminated_at/result_summary at completion (or by
    # the watchdog to 'force_terminated'). Mirrors loop_runs: UPDATE is
    # legitimate and granted; DELETE is revoked.
    op.execute(
        sa.text(
            """
            CREATE TABLE sub_agent_runs (
                id              UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
                sub_agent_id    UUID                NOT NULL REFERENCES sub_agents(id),
                session_id      UUID                NOT NULL REFERENCES sessions(id),
                loop_run_id     UUID                REFERENCES loop_runs(id),
                parent_agent    TEXT                NOT NULL,
                spawned_at      TIMESTAMPTZ         NOT NULL DEFAULT now(),
                terminated_at   TIMESTAMPTZ,
                status          sub_agent_status    NOT NULL DEFAULT 'spawned',
                result_summary  JSONB,
                error_detail    TEXT
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_sar_session   ON sub_agent_runs(session_id)"))
    op.execute(sa.text("CREATE INDEX idx_sar_sub_agent ON sub_agent_runs(sub_agent_id)"))
    op.execute(sa.text("CREATE INDEX idx_sar_status    ON sub_agent_runs(status)"))
    op.execute(sa.text("CREATE INDEX idx_sar_spawned   ON sub_agent_runs(spawned_at DESC)"))

    # ---- skill_chains -----------------------------------------------
    # Versioned catalog of production chains (Impl Plan §8.4). Has an
    # updated_at column, so it gets the shared update_updated_at_column
    # trigger (created in 0002). Never deleted; 'archived' is removal.
    # Version regex uses [0-9]/[.] (not \d/\.) to avoid backslash-escape
    # ambiguity between the Python string literal and the Postgres ARE.
    op.execute(
        sa.text(
            """
            CREATE TABLE skill_chains (
                id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
                name            TEXT            NOT NULL,
                version         TEXT            NOT NULL
                                                CHECK (version ~ '^[0-9]+[.][0-9]+[.][0-9]+$'),
                status          skill_status    NOT NULL DEFAULT 'draft',
                description     TEXT,
                skill_sequence  JSONB           NOT NULL,
                max_length      INTEGER         NOT NULL DEFAULT 5
                                                CHECK (max_length BETWEEN 1 AND 10),
                max_duration_ms INTEGER         NOT NULL DEFAULT 30000
                                                CHECK (max_duration_ms > 0),
                created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
                updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
                UNIQUE (name, version)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_sc_name   ON skill_chains(name)"))
    op.execute(sa.text("CREATE INDEX idx_sc_status ON skill_chains(status)"))
    op.execute(
        sa.text(
            """
            CREATE TRIGGER skill_chains_updated_at
                BEFORE UPDATE ON skill_chains
                FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()
            """
        )
    )

    # ---- skill_performance ------------------------------------------
    # Rolling per-skill metric windows. evaluative.skill_performance_tracker
    # UPSERTs on (skill_name, window_start, window_end), so both INSERT
    # and UPDATE are legitimate. Append-only vs the app deleting rows:
    # DELETE revoked (retention archival is out-of-band).
    op.execute(
        sa.text(
            """
            CREATE TABLE skill_performance (
                id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
                skill_name          TEXT            NOT NULL,
                window_start        TIMESTAMPTZ     NOT NULL,
                window_end          TIMESTAMPTZ     NOT NULL,
                invocation_count    INTEGER         NOT NULL DEFAULT 0
                                                    CHECK (invocation_count >= 0),
                accuracy_score      NUMERIC(5,4)    CHECK (accuracy_score BETWEEN 0 AND 1),
                p95_latency_ms      INTEGER         CHECK (p95_latency_ms >= 0),
                error_rate          NUMERIC(5,4)    CHECK (error_rate BETWEEN 0 AND 1),
                override_rate       NUMERIC(5,4)    CHECK (override_rate BETWEEN 0 AND 1),
                below_threshold     BOOLEAN         NOT NULL DEFAULT false,
                computed_at         TIMESTAMPTZ     NOT NULL DEFAULT now(),
                UNIQUE (skill_name, window_start, window_end)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_sp_skill_name ON skill_performance(skill_name)"))
    op.execute(
        sa.text(
            "CREATE INDEX idx_sp_threshold ON skill_performance(below_threshold) "
            "WHERE below_threshold = true"
        )
    )
    op.execute(sa.text("CREATE INDEX idx_sp_computed ON skill_performance(computed_at DESC)"))

    # ---- meta_loop_runs ---------------------------------------------
    # Meta-Loop scorecards. reviewed_by_user flips false->true on user
    # review, so UPDATE is granted; DELETE revoked (1-year trend history).
    op.execute(
        sa.text(
            """
            CREATE TABLE meta_loop_runs (
                id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                run_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
                lookback_days           INTEGER     NOT NULL DEFAULT 7
                                                    CHECK (lookback_days > 0),
                loop_health_scorecard   JSONB       NOT NULL DEFAULT '{}',
                anomalies_detected      JSONB       NOT NULL DEFAULT '[]',
                improvement_signals     JSONB       NOT NULL DEFAULT '[]',
                reviewed_by_user        BOOLEAN     NOT NULL DEFAULT false,
                triggered_by            TEXT        NOT NULL DEFAULT 'scheduled'
                                                    CHECK (triggered_by IN ('scheduled','manual','anomaly'))
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_mlr_run_at ON meta_loop_runs(run_at DESC)"))

    # ---- grants ------------------------------------------------------
    op.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE ON "
            f"sub_agents, sub_agent_runs, skill_chains, skill_performance, meta_loop_runs "
            f"TO {APP_ROLE}"
        )
    )
    # INV-02 (in spirit): the app never hard-deletes. DELETE is already
    # absent from the GRANT above; these explicit REVOKEs on the
    # append-only history tables mirror 0004's skill_invocation_log
    # REVOKE and make the intent unmistakable.
    op.execute(sa.text(f"REVOKE DELETE ON sub_agent_runs FROM {APP_ROLE}"))
    op.execute(sa.text(f"REVOKE DELETE ON skill_performance FROM {APP_ROLE}"))
    op.execute(sa.text(f"REVOKE DELETE ON meta_loop_runs FROM {APP_ROLE}"))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS meta_loop_runs"))
    op.execute(sa.text("DROP TABLE IF EXISTS skill_performance"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS skill_chains_updated_at ON skill_chains"))
    op.execute(sa.text("DROP TABLE IF EXISTS skill_chains"))
    op.execute(sa.text("DROP TABLE IF EXISTS sub_agent_runs"))
    op.execute(sa.text("DROP TABLE IF EXISTS sub_agents"))
