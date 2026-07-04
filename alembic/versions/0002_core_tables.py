"""0002 — pgvector extension, updated_at trigger fn, core tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-04

Tables: sessions, nodes, node_pillars, node_links.

Deviation from Section 9's literal table listing order ("Tables:
nodes, node_pillars, node_links, sessions"): ``nodes.session_id`` is a
foreign key into ``sessions.id``, so ``sessions`` must physically exist
first. Created in dependency order here: sessions -> nodes ->
node_pillars -> node_links. This does not change the resulting schema,
only the order of CREATE TABLE statements within this single
migration.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = "aether_app_role"


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))

    op.execute(
        sa.text(
            """
            CREATE FUNCTION update_updated_at_column()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = now();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )

    # ---- sessions --------------------------------------------------
    op.execute(
        sa.text(
            """
            CREATE TABLE sessions (
                id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                ended_at    TIMESTAMPTZ,
                status      session_status NOT NULL DEFAULT 'active',
                l1_snapshot JSONB,
                summary     TEXT
            )
            """
        )
    )

    # ---- nodes -------------------------------------------------------
    op.execute(
        sa.text(
            """
            CREATE TABLE nodes (
                id            UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
                type          node_type       NOT NULL,
                title         VARCHAR(120)    NOT NULL,
                content       TEXT            NOT NULL,
                source        node_source     NOT NULL,
                confidence    confidence_level NOT NULL,
                status        node_status     NOT NULL DEFAULT 'active',
                expiry_policy expiry_policy   NOT NULL DEFAULT 'permanent',
                expiry_date   TIMESTAMPTZ,
                created_by    created_by_agent NOT NULL,
                session_id    UUID            REFERENCES sessions(id),
                created_at    TIMESTAMPTZ     NOT NULL DEFAULT now(),
                updated_at    TIMESTAMPTZ     NOT NULL DEFAULT now(),
                metadata      JSONB           NOT NULL DEFAULT '{}'::jsonb,
                CONSTRAINT ck_nodes_title_length CHECK (length(title) <= 120)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_nodes_type ON nodes(type)"))
    op.execute(sa.text("CREATE INDEX idx_nodes_status ON nodes(status)"))
    op.execute(sa.text("CREATE INDEX idx_nodes_created ON nodes(created_at DESC)"))
    op.execute(sa.text("CREATE INDEX idx_nodes_metadata ON nodes USING GIN(metadata)"))
    op.execute(
        sa.text(
            """
            CREATE INDEX idx_nodes_search ON nodes
            USING GIN (to_tsvector('english', title || ' ' || content))
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX idx_nodes_deadline ON nodes ((metadata->>'deadline'))
            WHERE metadata->>'deadline' IS NOT NULL
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_nodes_tags ON nodes USING GIN((metadata->'tags'))"))
    op.execute(
        sa.text("CREATE INDEX idx_nodes_parties ON nodes USING GIN((metadata->'parties'))")
    )
    op.execute(
        sa.text(
            "CREATE INDEX idx_nodes_synth ON nodes USING GIN((metadata->'synthesis_from'))"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER nodes_updated_at
                BEFORE UPDATE ON nodes
                FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()
            """
        )
    )

    # ---- node_pillars --------------------------------------------
    op.execute(
        sa.text(
            """
            CREATE TABLE node_pillars (
                node_id     UUID        NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                pillar      pillar_name NOT NULL,
                is_primary  BOOLEAN     NOT NULL DEFAULT false,
                assigned_by created_by_agent NOT NULL,
                assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (node_id, pillar)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_node_pillars_pillar ON node_pillars(pillar)"))
    # Not explicitly in Section 5.1; added at the models checkpoint to
    # enforce "exactly one primary pillar per node" at the DB level.
    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX uq_node_pillars_one_primary ON node_pillars(node_id)
            WHERE is_primary = true
            """
        )
    )

    # ---- node_links --------------------------------------------------
    op.execute(
        sa.text(
            """
            CREATE TABLE node_links (
                id         UUID       PRIMARY KEY DEFAULT gen_random_uuid(),
                source_id  UUID       NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                target_id  UUID       NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                link_type  link_type  NOT NULL,
                created_by created_by_agent NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                notes      TEXT,
                CONSTRAINT uq_node_links_triplet UNIQUE (source_id, target_id, link_type)
            )
            """
        )
    )
    op.execute(sa.text("CREATE INDEX idx_node_links_source ON node_links(source_id)"))
    op.execute(sa.text("CREATE INDEX idx_node_links_target ON node_links(target_id)"))
    op.execute(sa.text("CREATE INDEX idx_node_links_type ON node_links(link_type)"))

    # ---- grants / INV-02 enforcement --------------------------------
    op.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE ON sessions, nodes, node_pillars, "
            f"node_links TO {APP_ROLE}"
        )
    )
    # INV-02: No Hard Deletion. DELETE was never granted above, but
    # this REVOKE is explicit and load-bearing documentation of intent
    # even though it is a no-op against the current privilege set.
    op.execute(sa.text(f"REVOKE DELETE ON nodes FROM {APP_ROLE}"))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS node_links"))
    op.execute(sa.text("DROP TABLE IF EXISTS node_pillars"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS nodes_updated_at ON nodes"))
    op.execute(sa.text("DROP TABLE IF EXISTS nodes"))
    op.execute(sa.text("DROP TABLE IF EXISTS sessions"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS update_updated_at_column"))
    op.execute(sa.text("DROP EXTENSION IF EXISTS vector"))
