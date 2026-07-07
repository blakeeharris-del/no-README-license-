"""
aether.models.logs
====================

ORM models for the append-only log tables: ``action_log``,
``synthesis_runs`` (Phase-0 Prompt Section 5.3).

INV-01 (Human Oversight Above All): every significant agent action is
recorded in ``action_log`` in plain language, BEFORE execution.
INV-06 (Authority Model Is Not Configurable at Runtime): ``user_confirmed``
may only be flipped to True by the ``/approve`` endpoint — never set
True at row-creation time by an agent.

Both the append-only-ness of ``action_log`` and the append-only-ness of
``skill_invocation_log`` (see ``runtime.py``) are enforced at the
database level via RLS policies (``REVOKE UPDATE, DELETE ... FROM
aether_app_role``), created in Alembic migrations 0003/0004. The ORM
models here intentionally do not expose any update/delete convenience
method for these rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from aether.models.base import Base
from aether.models.enums import ActionType, AgentName, pg_enum


class ActionLog(Base):
    """
    TABLE: action_log

    Append-only. No ``updated_at`` column by design — a correction to a
    logged action is a *new* row, never a mutation of an old one.
    """

    __tablename__ = "action_log"
    __table_args__ = (
        CheckConstraint(
            "input_summary IS NULL OR length(input_summary) <= 500",
            name="ck_action_log_input_summary_length",
        ),
        CheckConstraint(
            "output_summary IS NULL OR length(output_summary) <= 500",
            name="ck_action_log_output_summary_length",
        ),
        Index("idx_action_log_session", "session_id"),
        Index("idx_action_log_agent", "agent"),
        Index("idx_action_log_timestamp", "timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    agent: Mapped[AgentName] = mapped_column(pg_enum(AgentName, "agent_name"), nullable=False)
    action_type: Mapped[ActionType] = mapped_column(
        pg_enum(ActionType, "action_type"), nullable=False
    )
    node_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default=text("ARRAY[]::UUID[]"),
    )
    input_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_confirmed: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ActionLog id={self.id} agent={self.agent} "
            f"action_type={self.action_type} confirmed={self.user_confirmed}>"
        )


class DecisionRecord(Base):
    """
    TABLE: decision_journal (Phase-2, migration 0006).

    One row per exercised Decision Protocol run (Foundation §10.6;
    ``executive.decision_protocol``). This is the "Decision Journal" the
    Dashboard Spec already names as a Memory-zone data source. Records the
    full Sense -> Analyze -> Challenge sequence and whether the
    recommendation was deferred (§10.6 default).

    EC-38 confirmation mechanism (documented schema addition): the last
    three columns record that a decision's outcome was *confirmed correct*
    by an explicit, sourced confirmation — never synthesized. ``UPDATE`` is
    permitted only for the one NULL->set confirmation transition (grant in
    0006), mirroring how ``user_confirmed`` is set only by ``/approve``.
    The CHECK ``(confirmed_correct IS NULL OR confirmed_by IS NOT NULL)``
    forbids recording an outcome without naming who confirmed it — so a
    confirmation can never be anonymous or back-filled without a source
    (guards against the EC-19 "no synthetic ground truth" failure mode).
    """

    __tablename__ = "decision_journal"
    __table_args__ = (
        CheckConstraint(
            "confirmed_correct IS NULL OR confirmed_by IS NOT NULL",
            name="ck_dj_confirmation_sourced",
        ),
        Index("idx_dj_session", "session_id"),
        Index("idx_dj_confirmed", "confirmed_correct"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    proposed_action: Mapped[str] = mapped_column(Text, nullable=False)
    pillars: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    sense_summary: Mapped[str] = mapped_column(Text, nullable=False)
    analysis: Mapped[str] = mapped_column(Text, nullable=False)
    challenge: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    deferred: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    approval_required: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # --- EC-38 confirmation (NULL until an explicit, sourced confirmation) ---
    confirmed_correct: Mapped[Optional[bool]] = mapped_column(nullable=True)
    confirmed_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<DecisionRecord id={self.id} deferred={self.deferred} "
            f"confirmed={self.confirmed_correct}>"
        )


class SynthesisRun(Base):
    """TABLE: synthesis_runs"""

    __tablename__ = "synthesis_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    triggered_by: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        # 'scheduled' | 'manual' | 'threshold' — validated at the
        # application layer (Pydantic schema), not a DB enum, per spec
        # (Section 5.3 gives this as a plain str union, not one of the
        # 18 enum types).
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    nodes_processed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    nodes_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    diff_report: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    reviewed_by_user: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SynthesisRun id={self.id} triggered_by={self.triggered_by}>"
