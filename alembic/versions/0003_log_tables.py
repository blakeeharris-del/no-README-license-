"""0003 — action_log, synthesis_runs

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-04

INV-01: action_log is append-only in practice (application code never
issues UPDATE/DELETE against it), and enforced here at the DB level via
explicit REVOKE — UPDATE was granted by the blanket GRANT below, so
this REVOKE is not a no-op the way the DELETE revokes elsewhere are;
it actually removes a privilege the role would otherwise have.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "aether_app_role"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE action_log (
                id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                session_id     UUID        NOT NULL REFERENCES sessions(id),
                agent          agent_name  NOT NULL,
                action_type    action_type NOT NULL,
                node_ids       UUID[]      NOT NULL DEFAULT ARRAY[]::UUID[],
                input_summary  TEXT,
                output_summary TEXT,
                user_confirmed BOOLEAN     NOT NULL DEFAULT false,
                timestamp      TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_action_log_input_summary_length
                    CHECK (input_summary IS NULL OR length(input_summary) <= 500),
                CONSTRAINT ck_action_log_output_summary_length
                    CHECK (output_summary IS NULL OR length(output_summary) <= 500)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_action_log_session ON action_log(session_id)"))
    op.execute(sa.text("CREATE INDEX idx_action_log_agent ON action_log(agent)"))
    op.execute(sa.text("CREATE INDEX idx_action_log_timestamp ON action_log(timestamp)"))

    op.execute(
        sa.text(
            """
            CREATE TABLE synthesis_runs (
                id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                triggered_by     TEXT        NOT NULL,
                started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                completed_at     TIMESTAMPTZ,
                nodes_processed  INTEGER     NOT NULL DEFAULT 0,
                nodes_written    INTEGER     NOT NULL DEFAULT 0,
                diff_report      JSONB,
                reviewed_by_user BOOLEAN     NOT NULL DEFAULT false,
                CONSTRAINT ck_synthesis_runs_triggered_by
                    CHECK (triggered_by IN ('scheduled', 'manual', 'threshold'))
            )
            """
        )
    )

    op.execute(
        sa.text(f"GRANT SELECT, INSERT, UPDATE ON action_log, synthesis_runs TO {APP_ROLE}")
    )
    # INV-01: action_log is append-only. UPDATE was just granted above
    # by the blanket statement; this revokes it specifically for this
    # table. synthesis_runs is intentionally NOT append-only — its
    # `completed_at` / `reviewed_by_user` columns are updated in place
    # as a synthesis run progresses and is later reviewed.
    op.execute(sa.text(f"REVOKE UPDATE, DELETE ON action_log FROM {APP_ROLE}"))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS synthesis_runs"))
    op.execute(sa.text("DROP TABLE IF EXISTS action_log"))
