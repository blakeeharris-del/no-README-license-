"""0001 — application role + all enum types

Revision ID: 0001
Revises:
Create Date: 2026-07-04

Per Section 9: "All 18 PostgreSQL enum types [...] All 18 types must
exist before Migration 0002." As flagged at the models checkpoint, the
Phase-0 Prompt's own enum listing in Section 4 actually enumerates 19
distinct types; all 19 are created here (see
``aether.models.enums.PG_ENUM_TYPE_NAMES`` for the authoritative list
the ORM layer binds against).

Role setup ("RLS SETUP — run before migrations" in Section 9) is done
here, in migration 0001, since it must exist before any later
migration's per-table REVOKE statements. Note that ``GRANT SELECT,
INSERT, UPDATE ON ALL TABLES IN SCHEMA public`` cannot usefully run
here — no tables exist yet. Instead, each subsequent migration grants
SELECT/INSERT/UPDATE explicitly on the tables *it* creates, immediately
after creating them. DELETE is intentionally never granted to
``aether_app_role`` on any table, in any migration — INV-02 in spirit
applies to the whole schema, not just ``nodes``; the explicit
``REVOKE DELETE`` statements elsewhere in this migration set are
deliberate belt-and-suspenders, not the only enforcement.

Architectural decision, flagged for review: none of the three source
documents ever state that the *running application's* own DB
connection should authenticate as ``aether_app_role`` — every mention
of that role is about creating it and revoking privileges from it.
Taken completely literally, the app's connection (per .env.example's
original ``DATABASE_URL=...aether:password@...``) would run as the
unrestricted database owner, making every REVOKE in this migration set
practically inert for the actual running system, even though it would
still pass a test that connects as ``aether_app_role`` on purpose to
verify the grant. Foundation's own INV-02 text — "The database schema
enforces soft-delete at the schema level" — reads as a claim that
these are real, load-bearing runtime protections, not just tooling for
a future admin connection. Given that, this migration additionally
grants ``aether_app_role`` LOGIN and a password (from the
``AETHER_APP_DB_PASSWORD`` environment variable), and the app's own
``DATABASE_URL`` (see updated ``.env.example``) now authenticates as
that role. A separate ``MIGRATION_DATABASE_URL``, authenticating as
the unrestricted owner, is used only by ``alembic/env.py`` — migrations
themselves need CREATE TYPE/TABLE/ROLE privileges ``aether_app_role``
intentionally does not and should not have.
"""

from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Keep this in exact sync with aether.models.enums.PG_ENUM_TYPE_NAMES.
ENUM_TYPES: dict[str, list[str]] = {
    "node_type": ["fact", "event", "task", "goal", "reflection", "artifact", "signal"],
    "pillar_name": [
        "legal",
        "personal_finance",
        "career",
        "business",
        "health",
        "relationships",
    ],
    "confidence_level": ["explicit", "inferred", "speculative"],
    "node_status": ["active", "archived", "superseded", "flagged", "pending_review"],
    "node_source": ["user_explicit", "agent_write", "sync_import", "synthesis"],
    "expiry_policy": ["permanent", "review_after_date", "auto_expire_date"],
    "created_by_agent": ["user", "master_agent", "specialist_agent", "synthesis_agent"],
    "link_type": [
        "derives_from",
        "supersedes",
        "depends_on",
        "blocks",
        "related_to",
        "contradicts",
        "part_of",
    ],
    "action_type": ["read", "write", "route", "synthesize", "surface", "confirm"],
    "agent_name": [
        "master",
        "legal",
        "finance",
        "career",
        "business",
        "health",
        "relationships",
        "synthesis",
    ],
    "session_status": ["active", "closed", "error"],
    "skill_status": [
        "draft",
        "review",
        "validated",
        "staged",
        "active",
        "deprecated",
        "archived",
    ],
    "skill_category": [
        "cognitive",
        "analytical",
        "operational",
        "executive",
        "evaluative",
        "safety",
    ],
    "loop_type": [
        "goal",
        "reflection",
        "correction",
        "safety",
        "escalation",
        "shutdown",
        "meta",
    ],
    "loop_status": ["running", "completed", "failed", "forced_termination", "timeout"],
    "sub_agent_status": ["spawned", "running", "completed", "failed", "force_terminated"],
    "escalation_type": [
        "p0_signal",
        "correction_exhaust",
        "safety_alert",
        "clarification",
    ],
    "escalation_status": ["pending", "resolved", "expired"],
    "priority_class": ["p0", "p1", "p2", "p3", "suppress"],
}

APP_ROLE = "aether_app_role"


def upgrade() -> None:
    # Idempotent role creation: Alembic migrations are run exactly once
    # per environment in normal operation, but a DO block guards
    # against re-running against a partially-provisioned DB (e.g. a
    # role created by a DBA ahead of time).
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}'
                ) THEN
                    CREATE ROLE {APP_ROLE};
                END IF;
            END
            $$;
            """
        )
    )
    # current_database() must be resolved dynamically; GRANT CONNECT
    # ON DATABASE does not accept a function call directly, so this
    # goes through EXECUTE.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                EXECUTE format(
                    'GRANT CONNECT ON DATABASE %I TO {APP_ROLE}',
                    current_database()
                );
            END
            $$;
            """
        )
    )

    # Grant LOGIN + a password so the application can actually connect
    # as this restricted role (see module docstring's architectural
    # decision note). Read directly from the environment rather than
    # aether.config.settings — migrations run before config.py exists
    # in the build order (Step 7), and even after it exists, alembic's
    # own migration files are conventionally self-contained rather than
    # importing the application package. This is an intentional,
    # narrowly-scoped exception to coding convention #9 ("all config
    # via settings object"), justified by migrations being outside the
    # app's normal runtime import graph.
    #
    # The password is embedded as a literal (with standard SQL
    # quote-doubling), not a bind parameter — Postgres does not support
    # bind parameters inside DO blocks or most DDL at all (confirmed
    # empirically: `ALTER ROLE ... PASSWORD $1` raises a syntax error).
    # This is safe here because the value originates from our own
    # trusted environment configuration, not user input — the same
    # trust model already used for the enum label lists above.
    app_role_password = os.environ.get("AETHER_APP_DB_PASSWORD", "apppassword")
    escaped_password = app_role_password.replace("'", "''")
    op.execute(sa.text(f"ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD '{escaped_password}'"))

    for type_name, labels in ENUM_TYPES.items():
        label_list = ", ".join(f"'{label}'" for label in labels)
        op.execute(sa.text(f"CREATE TYPE {type_name} AS ENUM ({label_list})"))


def downgrade() -> None:
    # Drop enum types in reverse creation order (no cross-dependencies
    # between these types, so strict reverse order isn't load-bearing,
    # but it's the conservative default).
    for type_name in reversed(list(ENUM_TYPES.keys())):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {type_name}"))

    # The role still holds `GRANT CONNECT ON DATABASE` from upgrade();
    # DROP ROLE fails with "cannot be dropped because some objects
    # depend on it / privileges for database aether" unless that
    # database-level grant is revoked first. (Found by actually running
    # `alembic downgrade base` against real Postgres while verifying
    # this migration set — not obvious from a static read.)
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                EXECUTE format(
                    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM {APP_ROLE}',
                    current_database()
                );
            END
            $$;
            """
        )
    )
    op.execute(sa.text(f"DROP ROLE IF EXISTS {APP_ROLE}"))
