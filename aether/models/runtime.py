"""
aether.models.runtime
=======================

ORM models for the Phase-0 runtime/stub tables: ``skills``,
``skill_invocation_log``, ``loop_runs``, ``pending_escalations``
(Phase-0 Prompt Section 5.4).

Column-level definitions here follow AETHER_MISSING_SPECS_v1.0 Part 4
("Schema Update Pack") exactly, per that document's own instruction:
"Implement it exactly — those definitions are authoritative." Only the
4 of 9 Part-4 tables that are in-scope for Phase-0 are modeled here.
The other 5 (``sub_agents``, ``sub_agent_runs``, ``skill_chains``,
``skill_performance``, ``meta_loop_runs``) are out of scope per
Phase-0 Prompt Section 1.4 (no sub-agents, no skill chains, no
meta-loop in Phase-0) and are deferred to Phase-1+.

These tables are populated sparsely or not at all in Phase-0 (comment
in Section 5.4: "Populate in Phase-1+. Leave empty in Phase-0.") with
two exceptions that Phase-0 code paths do touch:
  - ``skill_invocation_log``: written by every ``invoke_skill()`` call
    (INV-09), starting in Phase-0.
  - ``loop_runs``: written by every ``GoalLoop`` run and monitored by
    the ``LoopWatchdog`` (INV-08), starting in Phase-0.

INV-08 (Every Loop Has a Maximum Iteration Count): enforced here at the
DB level via NOT NULL + CHECK on ``max_iterations`` / ``max_duration_ms``
on ``loop_runs`` — a loop_runs row simply cannot be inserted without
its bounds declared.

INV-09 (System State Is Always Recoverable / skill invocation
auditability): ``skill_invocation_log`` is append-only at the DB level
(RLS ``REVOKE UPDATE, DELETE`` in migration 0004); the ORM model
exposes no update/delete helper.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from aether.models.base import Base
from aether.models.enums import (
    EscalationStatus,
    EscalationType,
    LoopStatus,
    LoopType,
    PriorityClass,
    SkillCategory,
    SkillStatus,
    pg_enum,
)


class Skill(Base):
    """
    TABLE: skills

    Registry row for a named, versioned skill. INV-09 note: skill rows
    are never deleted; retirement is modeled as
    ``status = SkillStatus.ARCHIVED``.
    """

    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint(
            r"version ~ '^\d+\.\d+\.\d+(-\w+)?$'", name="ck_skills_version_semver"
        ),
        CheckConstraint("timeout_ms > 0", name="ck_skills_timeout_positive"),
        UniqueConstraint("name", "version", name="uq_skills_name_version"),
        Index("idx_skills_category", "category"),
        Index("idx_skills_status", "status"),
        Index("idx_skills_name", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[SkillCategory] = mapped_column(
        pg_enum(SkillCategory, "skill_category"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SkillStatus] = mapped_column(
        pg_enum(SkillStatus, "skill_status"),
        nullable=False,
        server_default=SkillStatus.DRAFT.value,
    )
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30000"))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    output_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Skill name={self.name} version={self.version} status={self.status}>"


class SkillInvocationLog(Base):
    """
    TABLE: skill_invocation_log

    Append-only IN SPIRIT (INV-09), enforced by a DB trigger
    (``sil_prevent_finalized_update``, added in migration 0004) that
    blocks any UPDATE to a row whose status is not ``'running'`` —
    rather than a blanket UPDATE revoke, which would have also blocked
    the one legitimate transition this table's own write pattern
    requires. Written by ``invoke_skill()``
    (``aether/skills/invoker.py``): one INSERT at START
    (``status='running'``), followed by exactly one UPDATE at
    COMPLETE/ERROR/TIMEOUT that sets the terminal status. DELETE
    remains fully revoked for the application role.
    """

    __tablename__ = "skill_invocation_log"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','ok','error','timeout')",
            name="ck_sil_status_values",
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_sil_latency_nonneg"),
        Index("idx_sil_session", "session_id"),
        Index("idx_sil_skill_name", "skill_name"),
        Index("idx_sil_status", "status"),
        Index("idx_sil_timestamp", "timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    skill_version: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'0.1.0-stub'")
    )
    invoked_by: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    loop_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loop_runs.id"), nullable=True
    )
    inputs_hash: Mapped[str] = mapped_column(Text, nullable=False)
    outputs_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SkillInvocationLog skill={self.skill_name} status={self.status}>"


class LoopRun(Base):
    """
    TABLE: loop_runs

    INV-08: ``max_iterations`` and ``max_duration_ms`` are NOT NULL and
    CHECK-constrained — a loop cannot be started without declared
    bounds. UPDATE is permitted on this table (unlike the append-only
    log tables): the LoopWatchdog updates ``status``, ``end_time``, and
    ``iteration_count`` as a loop progresses.
    """

    __tablename__ = "loop_runs"
    __table_args__ = (
        CheckConstraint("iteration_count >= 0", name="ck_loop_runs_iteration_nonneg"),
        CheckConstraint(
            "max_iterations IS NOT NULL AND max_duration_ms IS NOT NULL",
            name="ck_loop_runs_bounds_required",
        ),
        Index("idx_lr_session", "session_id"),
        Index("idx_lr_type", "loop_type"),
        Index("idx_lr_status", "status"),
        Index("idx_lr_start", "start_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    loop_type: Mapped[LoopType] = mapped_column(pg_enum(LoopType, "loop_type"), nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    parent_loop_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loop_runs.id"), nullable=True
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[LoopStatus] = mapped_column(
        pg_enum(LoopStatus, "loop_status"),
        nullable=False,
        server_default=LoopStatus.RUNNING.value,
    )
    iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    max_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LoopRun id={self.id} type={self.loop_type} status={self.status} "
            f"iter={self.iteration_count}/{self.max_iterations}>"
        )


class PendingEscalation(Base):
    """
    TABLE: pending_escalations

    DP-13 (Catch-Up on Return): any escalation surfaced while no
    session was active must still appear in the next session's opening
    brief — this table, plus ``status='pending'`` filtering in
    ``session_initializer``, is the enforcement mechanism.

    The partial-unique constraint below prevents duplicate pending P0
    escalations for the same node within the same session.
    """

    __tablename__ = "pending_escalations"
    __table_args__ = (
        Index("idx_pe_session", "session_id"),
        Index("idx_pe_priority", "priority_class"),
        Index("idx_pe_status", "status"),
        Index("idx_pe_created", "created_at"),
        Index(
            "uq_pe_p0_pending_per_node",
            "session_id",
            "priority_class",
            text("(content->>'node_id')"),
            unique=True,
            postgresql_where=text("priority_class = 'p0' AND status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    escalation_type: Mapped[EscalationType] = mapped_column(
        pg_enum(EscalationType, "escalation_type"), nullable=False
    )
    priority_class: Mapped[PriorityClass] = mapped_column(
        pg_enum(PriorityClass, "priority_class"), nullable=False
    )
    # Must contain at minimum {"title": str, "description": str}.
    # May also contain {"node_id", "loop_run_id", "skill_name"}.
    # Validated at the application layer (Pydantic), not by a DB CHECK,
    # since Postgres JSONB CHECK constraints on required-keys are
    # possible but brittle across jsonb_typeof edge cases; the
    # authoritative validation lives in the escalation-creation path.
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    loop_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loop_runs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    responded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    status: Mapped[EscalationStatus] = mapped_column(
        pg_enum(EscalationStatus, "escalation_status"),
        nullable=False,
        server_default=EscalationStatus.PENDING.value,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PendingEscalation id={self.id} type={self.escalation_type} "
            f"priority={self.priority_class} status={self.status}>"
        )
