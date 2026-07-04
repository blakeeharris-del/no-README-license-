"""
aether.models.enums
====================

All 18 PostgreSQL enum types, mirrored as Python ``str`` Enum classes.

Per Phase-0 Prompt Section 4: "Never use string literals anywhere in
codebase." Every place in the codebase that needs one of these values
must reference the Enum member, never the bare string.

Each enum here corresponds 1:1 to a ``CREATE TYPE ... AS ENUM (...)``
statement created in Alembic migration 0001. The Python member value
(the string on the right of ``=``) is exactly the Postgres enum label.
"""

from enum import Enum
from typing import Type

from sqlalchemy import Enum as SAEnum

# ---------------------------------------------------------------------------
# Native-Postgres-enum binding helper.
#
# SQLAlchemy's default behavior, given `Mapped[SomeEnumClass]`, is to
# auto-generate a native Postgres ENUM type (named after the lowercased
# Python class, e.g. "nodetype") AND to try to CREATE TYPE it itself
# whenever `Base.metadata.create_all()` runs. That is wrong for this
# project: Section 9 requires all enum types to be created exactly
# once, explicitly, in Alembic migration 0001 (`CREATE TYPE {name} AS
# ENUM (...)`), with a fixed snake_case name. If the ORM layer were
# also allowed to create (or auto-name) these types, migrations and
# models could silently drift apart.
#
# `pg_enum()` produces a SQLAlchemy Enum column type that (a) uses the
# exact snake_case type name the migration creates, and (b)
# `create_type=False`, so the ORM only ever *references* a type that
# migration 0001 is solely responsible for creating.
# ---------------------------------------------------------------------------


def pg_enum(python_enum: Type[Enum], pg_type_name: str) -> SAEnum:
    """Bind a Python ``str`` Enum to an existing native Postgres enum type.

    Args:
        python_enum: One of the Enum classes defined in this module.
        pg_type_name: The exact name of the Postgres enum type as
            created by ``CREATE TYPE {pg_type_name} AS ENUM (...)`` in
            Alembic migration 0001.

    Critical detail: SQLAlchemy's default behavior for a native
    ``Enum`` column bound to a Python ``Enum`` class is to send the
    member's *name* (e.g. ``"ACTIVE"``) to the database, not its
    ``.value`` (``"active"``) — this only becomes visible the first
    time a row is actually inserted, since it does not surface as a
    Python-side or DDL-compile-time error. Every Postgres enum label in
    this codebase is the lowercase ``.value``, so ``values_callable``
    is required here, or every INSERT touching an enum column would
    fail at runtime with "invalid input value for enum".
    """
    return SAEnum(
        python_enum,
        name=pg_type_name,
        native_enum=True,
        create_type=False,
        validate_strings=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


# Canonical Python-Enum-class -> Postgres-type-name mapping. Alembic
# migration 0001 (Section 9) must create exactly these types, with
# exactly these names. Every type must exist before any table in
# migration 0002+ references it.
PG_ENUM_TYPE_NAMES: dict[str, str] = {
    "NodeType": "node_type",
    "PillarName": "pillar_name",
    "ConfidenceLevel": "confidence_level",
    "NodeStatus": "node_status",
    "NodeSource": "node_source",
    "ExpiryPolicy": "expiry_policy",
    "CreatedByAgent": "created_by_agent",
    "LinkType": "link_type",
    "ActionType": "action_type",
    "AgentName": "agent_name",
    "SessionStatus": "session_status",
    "SkillStatus": "skill_status",
    "SkillCategory": "skill_category",
    "LoopType": "loop_type",
    "LoopStatus": "loop_status",
    "SubAgentStatus": "sub_agent_status",
    "EscalationType": "escalation_type",
    "EscalationStatus": "escalation_status",
    "PriorityClass": "priority_class",
}


class NodeType(str, Enum):
    """Type of a memory node (nodes.type)."""

    FACT = "fact"
    EVENT = "event"
    TASK = "task"
    GOAL = "goal"
    REFLECTION = "reflection"
    ARTIFACT = "artifact"
    SIGNAL = "signal"


class PillarName(str, Enum):
    """One of the six life pillars (Foundation §5)."""

    LEGAL = "legal"
    PERSONAL_FINANCE = "personal_finance"
    CAREER = "career"
    BUSINESS = "business"
    HEALTH = "health"
    RELATIONSHIPS = "relationships"


class ConfidenceLevel(str, Enum):
    """
    Confidence tier of a memory node.

    INV-03: confidence may only be *increased* via explicit user
    confirmation; it is never agent-promoted.
    """

    EXPLICIT = "explicit"        # user-confirmed, or sync_import only
    INFERRED = "inferred"        # agent-derived, >= 3 corroborating nodes
    SPECULATIVE = "speculative"  # agent-derived, unconfirmed or < 3 nodes


class NodeStatus(str, Enum):
    """
    Lifecycle status of a memory node.

    INV-02: DELETED is intentionally not a member of this enum.
    Nodes are never hard-deleted; only soft-transitioned between
    these statuses.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    FLAGGED = "flagged"
    PENDING_REVIEW = "pending_review"


class NodeSource(str, Enum):
    """Provenance of how a node entered the system."""

    USER_EXPLICIT = "user_explicit"  # direct user input
    AGENT_WRITE = "agent_write"      # agent-derived (inferred/speculative only)
    SYNC_IMPORT = "sync_import"      # external import (explicit allowed)
    SYNTHESIS = "synthesis"          # synthesis cycle output


class ExpiryPolicy(str, Enum):
    """When/whether a node's relevance expires."""

    PERMANENT = "permanent"
    REVIEW_AFTER_DATE = "review_after_date"
    AUTO_EXPIRE_DATE = "auto_expire_date"


class CreatedByAgent(str, Enum):
    """Which actor created a node / link / pillar assignment."""

    USER = "user"
    MASTER_AGENT = "master_agent"
    SPECIALIST_AGENT = "specialist_agent"
    SYNTHESIS_AGENT = "synthesis_agent"


class LinkType(str, Enum):
    """Typed, directed relationship between two nodes (node_links.link_type)."""

    DERIVES_FROM = "derives_from"
    SUPERSEDES = "supersedes"
    DEPENDS_ON = "depends_on"
    BLOCKS = "blocks"
    RELATED_TO = "related_to"
    CONTRADICTS = "contradicts"
    PART_OF = "part_of"


class ActionType(str, Enum):
    """Category of action recorded in action_log."""

    READ = "read"
    WRITE = "write"
    ROUTE = "route"
    SYNTHESIZE = "synthesize"
    SURFACE = "surface"
    CONFIRM = "confirm"


class AgentName(str, Enum):
    """Identity of an agent for logging/authority purposes."""

    MASTER = "master"
    LEGAL = "legal"
    FINANCE = "finance"
    CAREER = "career"
    BUSINESS = "business"
    HEALTH = "health"
    RELATIONSHIPS = "relationships"
    SYNTHESIS = "synthesis"


class SessionStatus(str, Enum):
    """Lifecycle status of a session."""

    ACTIVE = "active"
    CLOSED = "closed"
    ERROR = "error"


class SkillStatus(str, Enum):
    """Lifecycle status of a registered skill."""

    DRAFT = "draft"
    REVIEW = "review"
    VALIDATED = "validated"
    STAGED = "staged"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class SkillCategory(str, Enum):
    """The six skill categories (Foundation glossary: Skill)."""

    COGNITIVE = "cognitive"
    ANALYTICAL = "analytical"
    OPERATIONAL = "operational"
    EXECUTIVE = "executive"
    EVALUATIVE = "evaluative"
    SAFETY = "safety"


class LoopType(str, Enum):
    """The seven loop types coordinated by the Loop Engine."""

    GOAL = "goal"
    REFLECTION = "reflection"
    CORRECTION = "correction"
    SAFETY = "safety"
    ESCALATION = "escalation"
    SHUTDOWN = "shutdown"
    META = "meta"


class LoopStatus(str, Enum):
    """Terminal/in-flight status of a loop_runs row."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    FORCED_TERMINATION = "forced_termination"
    TIMEOUT = "timeout"


class SubAgentStatus(str, Enum):
    """Status of a sub_agent_runs row. Not used until Phase-1 (no sub-agents in Phase-0)."""

    SPAWNED = "spawned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    FORCE_TERMINATED = "force_terminated"


class EscalationType(str, Enum):
    """Reason category for a pending_escalations row."""

    P0_SIGNAL = "p0_signal"
    CORRECTION_EXHAUST = "correction_exhaust"
    SAFETY_ALERT = "safety_alert"
    CLARIFICATION = "clarification"


class EscalationStatus(str, Enum):
    """Lifecycle status of a pending_escalations row."""

    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class PriorityClass(str, Enum):
    """Signal-scoring priority class (Cognitive skill: signal_scorer)."""

    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"
    SUPPRESS = "suppress"
