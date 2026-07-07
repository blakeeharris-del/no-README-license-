"""0007 — trust-advance marker must be sourced (Phase-2, EC-35)

Revision ID: 0007
Revises: 0006
Create Date: Phase-2 (Gate G2 -> G3)

Blake's ruling: no trust stage advances without an explicit source. The
stage is recorded as an ``action_log`` marker ``trust_maturity Tx->Ty:
confirmed_by=<source>; ...`` (the gate reads it live via
``current_trust_stage``). This CHECK makes an unsourced advance
*impossible at the database level* — not merely discouraged — mirroring
the ``decision_journal`` confirmation-sourced discipline: any
``trust_maturity`` transition marker MUST contain ``confirmed_by=``.

Non-trust action_log rows are unaffected (the LIKE only matches the
'trust_maturity <from>-><to>' marker shape). No committed action_log row
is a trust marker (Phase-1's T0->T1 advance ran only in rolled-back test
transactions), so ADD CONSTRAINT validates cleanly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE action_log
            ADD CONSTRAINT ck_action_log_trust_marker_sourced
            CHECK (
                output_summary IS NULL
                OR output_summary NOT LIKE 'trust_maturity %->%'
                OR output_summary LIKE '%confirmed_by=%'
            )
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("ALTER TABLE action_log DROP CONSTRAINT IF EXISTS ck_action_log_trust_marker_sourced")
    )
