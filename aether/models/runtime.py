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

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
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
    SubAgentStatus,
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


# =====================================================================
# Phase-1 agent-ecosystem tables (migration 0005)
# =====================================================================


class SubAgent(Base):
    """
    TABLE: sub_agents

    Registry/catalog of the 30 sub-agents (AGENT_ARCHITECTURE §5-6,
    Missing Specs Volume 2). One row per sub-agent *definition*; a
    single definition is invoked many times, each producing one
    ``sub_agent_runs`` row.

    ``status`` is a TEXT+CHECK column (not a native enum) per the
    schema — the three lifecycle values are validated by the DB CHECK
    below and, at the app layer, by the Pydantic schema.
    """

    __tablename__ = "sub_agents"
    __table_args__ = (
        CheckConstraint("max_duration_ms > 0", name="ck_sub_agents_duration"),
        CheckConstraint("max_iterations > 0", name="ck_sub_agents_iterations"),
        CheckConstraint("authority_level BETWEEN 0 AND 5", name="ck_sub_agents_authority"),
        CheckConstraint("phase_introduced BETWEEN 0 AND 3", name="ck_sub_agents_phase"),
        CheckConstraint(
            "status IN ('active','deprecated','inactive')", name="ck_sub_agents_status"
        ),
        Index("idx_sub_agents_parent", "parent_agent"),
        Index("idx_sub_agents_domain", "domain"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    parent_agent: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trigger_event: Mapped[str] = mapped_column(Text, nullable=False)
    termination_condition: Mapped[str] = mapped_column(Text, nullable=False)
    max_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("60000"))
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    authority_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    phase_introduced: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SubAgent name={self.name} parent={self.parent_agent} status={self.status}>"


class SubAgentRun(Base):
    """
    TABLE: sub_agent_runs

    One row per sub-agent invocation. Inserted at spawn with
    status='spawned'; UPDATEd to a terminal status (or by the watchdog
    to 'force_terminated') at completion. This — not ``action_log`` —
    is the log EC-18 requires every sub-agent invocation to land in
    (Foundation §10.5 Agent Communication Protocol).
    """

    __tablename__ = "sub_agent_runs"
    __table_args__ = (
        Index("idx_sar_session", "session_id"),
        Index("idx_sar_sub_agent", "sub_agent_id"),
        Index("idx_sar_status", "status"),
        Index("idx_sar_spawned", "spawned_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    sub_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sub_agents.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    loop_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loop_runs.id"), nullable=True
    )
    parent_agent: Mapped[str] = mapped_column(Text, nullable=False)
    spawned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    terminated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[SubAgentStatus] = mapped_column(
        pg_enum(SubAgentStatus, "sub_agent_status"),
        nullable=False,
        server_default=SubAgentStatus.SPAWNED.value,
    )
    result_summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SubAgentRun id={self.id} status={self.status}>"


class SkillChain(Base):
    """
    TABLE: skill_chains

    Versioned catalog of named production chains (Impl Plan §8.4). The
    five Phase-1 chains live here. ``skill_sequence`` is the ordered
    JSONB list of {skill_name, skill_version, input_binding, required}.
    Never hard-deleted; status='archived' is removal. ``updated_at`` is
    maintained by the shared DB trigger, not the app.
    """

    __tablename__ = "skill_chains"
    __table_args__ = (
        CheckConstraint(
            "version ~ '^[0-9]+[.][0-9]+[.][0-9]+$'", name="ck_skill_chains_version"
        ),
        CheckConstraint("max_length BETWEEN 1 AND 10", name="ck_skill_chains_max_length"),
        CheckConstraint("max_duration_ms > 0", name="ck_skill_chains_duration"),
        UniqueConstraint("name", "version", name="uq_skill_chains_name_version"),
        Index("idx_sc_name", "name"),
        Index("idx_sc_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SkillStatus] = mapped_column(
        pg_enum(SkillStatus, "skill_status"),
        nullable=False,
        server_default=SkillStatus.DRAFT.value,
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    skill_sequence: Mapped[list] = mapped_column(JSONB, nullable=False)
    max_length: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    max_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30000"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SkillChain name={self.name} v{self.version} status={self.status}>"


class SkillPerformance(Base):
    """
    TABLE: skill_performance

    Rolling per-skill metric windows, UPSERTed by
    evaluative.skill_performance_tracker on
    (skill_name, window_start, window_end). Feeds EC-20 and the
    Meta-Loop scorecard (EC-24).
    """

    __tablename__ = "skill_performance"
    __table_args__ = (
        CheckConstraint("invocation_count >= 0", name="ck_sp_invocation_count"),
        CheckConstraint("accuracy_score BETWEEN 0 AND 1", name="ck_sp_accuracy"),
        CheckConstraint("p95_latency_ms >= 0", name="ck_sp_latency"),
        CheckConstraint("error_rate BETWEEN 0 AND 1", name="ck_sp_error_rate"),
        CheckConstraint("override_rate BETWEEN 0 AND 1", name="ck_sp_override_rate"),
        UniqueConstraint("skill_name", "window_start", "window_end", name="uq_sp_window"),
        Index("idx_sp_skill_name", "skill_name"),
        Index(
            "idx_sp_threshold",
            "below_threshold",
            postgresql_where=text("below_threshold = true"),
        ),
        Index("idx_sp_computed", "computed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invocation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    accuracy_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 4), nullable=True)
    p95_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_rate: Mapped[Optional[float]] = mapped_column(Numeric(5, 4), nullable=True)
    override_rate: Mapped[Optional[float]] = mapped_column(Numeric(5, 4), nullable=True)
    below_threshold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SkillPerformance skill={self.skill_name} below_threshold={self.below_threshold}>"


class MetaLoopRun(Base):
    """
    TABLE: meta_loop_runs

    Meta-Loop health scorecards (EC-24). ``reviewed_by_user`` flips
    false->true on user review. ``triggered_by`` is a TEXT+CHECK
    column ('scheduled'|'manual'|'anomaly').
    """

    __tablename__ = "meta_loop_runs"
    __table_args__ = (
        CheckConstraint("lookback_days > 0", name="ck_mlr_lookback"),
        CheckConstraint(
            "triggered_by IN ('scheduled','manual','anomaly')", name="ck_mlr_triggered_by"
        ),
        Index("idx_mlr_run_at", "run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("7"))
    loop_health_scorecard: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    anomalies_detected: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    improvement_signals: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    reviewed_by_user: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    triggered_by: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'scheduled'")
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MetaLoopRun id={self.id} run_at={self.run_at} triggered_by={self.triggered_by}>"
