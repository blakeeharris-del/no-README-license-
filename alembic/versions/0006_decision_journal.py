"""0006 — decision_journal (Phase-2, EC-38)

Revision ID: 0006
Revises: 0005
Create Date: Phase-2 (Gate G2 -> G3)

Adds the ``decision_journal`` table backing EC-38: the Decision Protocol
(Foundation §10.6, ``executive.decision_protocol``) exercised on real
decisions with *confirmed accuracy*.

Documented schema addition (per the Phase-2 instruction to propose and
note a schema addition when recording genuine confirmation requires one).
It is not an invented table — the AETHER_DASHBOARD_SPEC_v1.0 Memory zone
already names a "Decision Journal" as a data source; this gives it a home.

Confirmation model: ``confirmed_correct`` is NULL until an explicit,
sourced confirmation is recorded. The CHECK forbids recording an outcome
without ``confirmed_by`` (no anonymous / back-filled ground truth —
guards the EC-19 "no synthetic accuracy" failure mode). UPDATE is granted
so the one NULL->set confirmation transition can be written (mirroring how
``user_confirmed`` is set only by ``/approve``); DELETE is never granted
(INV-02 in spirit).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = "aether_app_role"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE decision_journal (
                id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                session_id        UUID        NOT NULL REFERENCES sessions(id),
                proposed_action   TEXT        NOT NULL,
                pillars           JSONB       NOT NULL DEFAULT '[]'::jsonb,
                sense_summary     TEXT        NOT NULL,
                analysis          TEXT        NOT NULL,
                challenge         TEXT        NOT NULL,
                recommendation    TEXT        NOT NULL,
                deferred          BOOLEAN     NOT NULL DEFAULT true,
                approval_required BOOLEAN     NOT NULL DEFAULT false,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                confirmed_correct BOOLEAN,
                confirmed_by      TEXT,
                confirmed_at      TIMESTAMPTZ,
                CONSTRAINT ck_dj_confirmation_sourced
                    CHECK (confirmed_correct IS NULL OR confirmed_by IS NOT NULL)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_dj_session ON decision_journal(session_id)"))
    op.execute(sa.text("CREATE INDEX idx_dj_confirmed ON decision_journal(confirmed_correct)"))

    # SELECT/INSERT/UPDATE (UPDATE only for the confirmation transition);
    # never DELETE (INV-02 in spirit).
    op.execute(
        sa.text(f"GRANT SELECT, INSERT, UPDATE ON decision_journal TO {APP_ROLE}")
    )
    op.execute(sa.text(f"REVOKE DELETE ON decision_journal FROM {APP_ROLE}"))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS decision_journal"))
