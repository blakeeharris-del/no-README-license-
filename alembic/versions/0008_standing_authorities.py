"""0008 — standing_authorities (Phase-2, EC-36)

Revision ID: 0008
Revises: 0007
Create Date: Phase-2 (Gate G2 -> G3)

The standing-authority store (Foundation §9.2 T3; approved Proposal 3).
A grant lets a (pillar, action_type) run without per-action approval while
global trust >= T3. Per-pillar scoped; reversible-only; Blake-authored;
periodically renewed; revoked (not deleted).

Structural guards baked into the schema:
  - reversible CHECK = true   -> only reversible actions qualify (§9.2, DP-08)
  - granted_by NOT NULL       -> a grant with no source is impossible
                                 (the anti-inference guard)
  - renewal_date NOT NULL     -> §9.2 periodic renewal
  - status CHECK in (active/lapsed/revoked); DELETE never granted (INV-02)

pillar_name enum already exists (migration 0001).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP_ROLE = "aether_app_role"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE standing_authorities (
                id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                pillar        pillar_name NOT NULL,
                action_type   TEXT        NOT NULL,
                bounds        JSONB       NOT NULL DEFAULT '{}'::jsonb,
                reversible    BOOLEAN     NOT NULL,
                rationale     TEXT        NOT NULL,
                granted_by    TEXT        NOT NULL,
                granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                renewal_date  TIMESTAMPTZ NOT NULL,
                status        TEXT        NOT NULL DEFAULT 'active',
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_sa_reversible_only CHECK (reversible = true),
                CONSTRAINT ck_sa_status CHECK (status IN ('active','lapsed','revoked'))
            )
            """
        )
    )
    op.execute(
        sa.text("CREATE INDEX idx_sa_lookup ON standing_authorities(pillar, action_type, status)")
    )
    # SELECT/INSERT/UPDATE (UPDATE for status active->lapsed/revoked); never
    # DELETE (INV-02 — a grant is revoked, not erased; the record is permanent).
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE ON standing_authorities TO {APP_ROLE}"))
    op.execute(sa.text(f"REVOKE DELETE ON standing_authorities FROM {APP_ROLE}"))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS standing_authorities"))
