"""0004 — skills, skill_invocation_log, loop_runs, pending_escalations

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-04

Only the 4 of 9 AETHER_MISSING_SPECS_v1.0 Part 4 tables that are
in-scope for Phase-0 are created here (see the models checkpoint note
in aether/models/runtime.py for the full scope rationale).

Two deliberate deviations from a literal reading of the source specs,
both flagged for review:

1. skill_invocation_log RLS: Missing Specs Part 4 gives this table
   real Postgres Row-Level-Security policies (`ENABLE ROW LEVEL
   SECURITY` + `CREATE POLICY ... FOR UPDATE ... USING (false)` +
   same for DELETE), but defines no permissive SELECT/INSERT policy.
   Enabling RLS with only those two restrictive policies and no
   permissive one would block ALL access for a non-owner role,
   including the SELECT/INSERT that invoke_skill() needs for every
   single invocation (INV-09) — that would be a functional bug if
   applied verbatim. Phase-0 Prompt Section 9 describes the same
   requirement more simply as "RLS: REVOKE UPDATE, DELETE ON
   skill_invocation_log FROM aether_app_role" — identical phrasing to
   how it describes action_log's append-only enforcement, which is
   implemented via plain GRANT/REVOKE, not real RLS. This migration
   follows that simpler, non-breaking interpretation for consistency
   with action_log, rather than the literal (and here, broken) RLS
   policy text.

2. pending_escalations' partial-uniqueness rule ("UNIQUE NULLS NOT
   DISTINCT (session_id, priority_class, (content->>'node_id')) WHERE
   priority_class = 'p0' AND status = 'pending'") is written in
   Missing Specs as an inline table CONSTRAINT, but Postgres does not
   support a WHERE clause on a table-level UNIQUE constraint — a
   partial-unique rule can only be expressed as a CREATE UNIQUE INDEX
   ... WHERE .... Implemented here as such (functionally identical
   intent, valid syntax).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "aether_app_role"


def upgrade() -> None:
    # ---- skills ----------------------------------------------------
    op.execute(
        sa.text(
            r"""
            CREATE TABLE skills (
                id            UUID           PRIMARY KEY DEFAULT gen_random_uuid(),
                name          TEXT           NOT NULL,
                category      skill_category NOT NULL,
                version       TEXT           NOT NULL,
                status        skill_status   NOT NULL DEFAULT 'draft',
                timeout_ms    INTEGER        NOT NULL DEFAULT 30000,
                description   TEXT,
                input_schema  JSONB          NOT NULL DEFAULT '{}'::jsonb,
                output_schema JSONB          NOT NULL DEFAULT '{}'::jsonb,
                created_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),
                updated_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),
                CONSTRAINT ck_skills_version_semver
                    CHECK (version ~ '^\d+\.\d+\.\d+(-\w+)?$'),
                CONSTRAINT ck_skills_timeout_positive CHECK (timeout_ms > 0),
                CONSTRAINT uq_skills_name_version UNIQUE (name, version)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_skills_category ON skills(category)"))
    op.execute(sa.text("CREATE INDEX idx_skills_status ON skills(status)"))
    op.execute(sa.text("CREATE INDEX idx_skills_name ON skills(name)"))
    op.execute(
        sa.text(
            """
            CREATE TRIGGER skills_updated_at
                BEFORE UPDATE ON skills
                FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()
            """
        )
    )

    # ---- skill_invocation_log --------------------------------------
    op.execute(
        sa.text(
            """
            CREATE TABLE skill_invocation_log (
                id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                skill_name    TEXT        NOT NULL,
                skill_version TEXT        NOT NULL DEFAULT '0.1.0-stub',
                invoked_by    TEXT        NOT NULL,
                session_id    UUID        NOT NULL REFERENCES sessions(id),
                loop_run_id   UUID,
                inputs_hash   TEXT        NOT NULL,
                outputs_hash  TEXT,
                status        TEXT        NOT NULL DEFAULT 'running',
                latency_ms    INTEGER,
                error_detail  TEXT,
                timestamp     TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_sil_status_values
                    CHECK (status IN ('running','ok','error','timeout')),
                CONSTRAINT ck_sil_latency_nonneg
                    CHECK (latency_ms IS NULL OR latency_ms >= 0)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_sil_session ON skill_invocation_log(session_id)"))
    op.execute(sa.text("CREATE INDEX idx_sil_skill_name ON skill_invocation_log(skill_name)"))
    op.execute(sa.text("CREATE INDEX idx_sil_status ON skill_invocation_log(status)"))
    op.execute(sa.text("CREATE INDEX idx_sil_timestamp ON skill_invocation_log(timestamp)"))

    # Real conflict, found only by actually running invoke_skill()
    # against the fully-enforced restricted role (see the skills-layer
    # checkpoint): Section 13's invoke_skill() INSERTs a 'running' row
    # at START and UPDATEs it to its terminal status at
    # COMPLETE/ERROR/TIMEOUT (INV-09's crash-visibility design) — but
    # this migration's original blanket `REVOKE UPDATE` made that
    # UPDATE fail for the app's real connection once it genuinely
    # authenticates as aether_app_role. A trigger resolves the
    # underlying tension correctly: it allows exactly the one
    # legitimate running->terminal transition invoke_skill() needs,
    # while still blocking any edit to an already-finalized row — the
    # actual *intent* behind "append-only" (no tampering with a
    # completed audit entry), rather than the letter of a blanket
    # REVOKE that turned out to block the system's own normal
    # operation.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION prevent_finalized_skill_log_update()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.status != 'running' THEN
                    RAISE EXCEPTION
                        'skill_invocation_log row % is already finalized (status=%) and cannot be modified',
                        OLD.id, OLD.status;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER sil_prevent_finalized_update
                BEFORE UPDATE ON skill_invocation_log
                FOR EACH ROW EXECUTE FUNCTION prevent_finalized_skill_log_update()
            """
        )
    )

    # ---- loop_runs ---------------------------------------------------
    op.execute(
        sa.text(
            """
            CREATE TABLE loop_runs (
                id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                loop_type          loop_type   NOT NULL,
                trigger            TEXT        NOT NULL,
                session_id         UUID        REFERENCES sessions(id),
                parent_loop_run_id UUID        REFERENCES loop_runs(id),
                start_time         TIMESTAMPTZ NOT NULL DEFAULT now(),
                end_time           TIMESTAMPTZ,
                status             loop_status NOT NULL DEFAULT 'running',
                iteration_count    INTEGER     NOT NULL DEFAULT 0,
                max_iterations     INTEGER     NOT NULL,
                max_duration_ms    INTEGER     NOT NULL,
                notes              TEXT,
                CONSTRAINT ck_loop_runs_iteration_nonneg CHECK (iteration_count >= 0),
                CONSTRAINT ck_loop_runs_bounds_required
                    CHECK (max_iterations IS NOT NULL AND max_duration_ms IS NOT NULL)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_lr_session ON loop_runs(session_id)"))
    op.execute(sa.text("CREATE INDEX idx_lr_type ON loop_runs(loop_type)"))
    op.execute(sa.text("CREATE INDEX idx_lr_status ON loop_runs(status)"))
    op.execute(sa.text("CREATE INDEX idx_lr_start ON loop_runs(start_time)"))

    # skill_invocation_log.loop_run_id FK added now that loop_runs exists.
    op.execute(
        sa.text(
            """
            ALTER TABLE skill_invocation_log
                ADD CONSTRAINT fk_sil_loop_run FOREIGN KEY (loop_run_id)
                REFERENCES loop_runs(id)
            """
        )
    )

    # ---- pending_escalations ----------------------------------------
    op.execute(
        sa.text(
            """
            CREATE TABLE pending_escalations (
                id              UUID               PRIMARY KEY DEFAULT gen_random_uuid(),
                escalation_type escalation_type    NOT NULL,
                priority_class  priority_class     NOT NULL,
                content         JSONB              NOT NULL,
                session_id      UUID               REFERENCES sessions(id),
                loop_run_id     UUID               REFERENCES loop_runs(id),
                created_at      TIMESTAMPTZ        NOT NULL DEFAULT now(),
                responded_at    TIMESTAMPTZ,
                response        JSONB,
                status          escalation_status  NOT NULL DEFAULT 'pending'
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_pe_session ON pending_escalations(session_id)"))
    op.execute(sa.text("CREATE INDEX idx_pe_priority ON pending_escalations(priority_class)"))
    op.execute(sa.text("CREATE INDEX idx_pe_status ON pending_escalations(status)"))
    op.execute(sa.text("CREATE INDEX idx_pe_created ON pending_escalations(created_at)"))
    # Partial-unique rule expressed as an index (see module docstring
    # deviation #2).
    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX uq_pe_p0_pending_per_node ON pending_escalations (
                session_id, priority_class, (content->>'node_id')
            )
            NULLS NOT DISTINCT
            WHERE priority_class = 'p0' AND status = 'pending'
            """
        )
    )

    # ---- grants ------------------------------------------------------
    op.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE ON skills, skill_invocation_log, "
            f"loop_runs, pending_escalations TO {APP_ROLE}"
        )
    )
    # INV-09: skill_invocation_log is append-only IN SPIRIT — enforced
    # by the trigger above (which blocks editing an already-finalized
    # row), not by a blanket UPDATE revoke, since invoke_skill() itself
    # needs exactly one legitimate UPDATE per row (running -> terminal
    # status). DELETE remains fully revoked; there is no legitimate
    # reason for any row in this table to ever be deleted.
    op.execute(sa.text(f"REVOKE DELETE ON skill_invocation_log FROM {APP_ROLE}"))
    # loop_runs UPDATE is deliberately left granted: the LoopWatchdog
    # updates status/end_time/iteration_count as a loop progresses.


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS pending_escalations"))
    op.execute(
        sa.text("ALTER TABLE skill_invocation_log DROP CONSTRAINT IF EXISTS fk_sil_loop_run")
    )
    op.execute(sa.text("DROP TABLE IF EXISTS loop_runs"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS sil_prevent_finalized_update ON skill_invocation_log"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS prevent_finalized_skill_log_update"))
    op.execute(sa.text("DROP TABLE IF EXISTS skill_invocation_log"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS skills_updated_at ON skills"))
    op.execute(sa.text("DROP TABLE IF EXISTS skills"))
