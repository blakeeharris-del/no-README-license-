"""
aether.models.nodes
====================

ORM models for the core memory tables: ``nodes``, ``node_pillars``,
``node_links`` (Phase-0 Prompt Section 5.1).

INV-02 (No Hard Deletion): the ``nodes`` table has no DELETE grant for
the application role. That grant is revoked at the DB level in
Alembic migration 0002 via RLS: ``REVOKE DELETE ON nodes FROM
aether_app_role``. Nodes are only ever soft-transitioned between
``NodeStatus`` values — the ORM model intentionally exposes no delete
helper.

INV-03 (Confidence Is Never Agent-Promoted): the DB schema does not by
itself enforce that ``confidence == EXPLICIT`` implies
``created_by == USER`` or ``source == SYNC_IMPORT``. That pairing is
an application-layer guard, enforced in ``write_node()``
(``aether/memory/write_protocol.py``, Section 10) and in
``aether/invariants/guards.py`` (Section 8).

Note on the ``metadata`` column: ``Node.metadata`` would collide with
SQLAlchemy's reserved ``Base.metadata`` (the table/schema registry)
attribute. The Python attribute is therefore named ``metadata_`` and
mapped explicitly to the ``metadata`` column via
``mapped_column("metadata", ...)`` so the wire/DB name matches the
spec exactly while the Python attribute name stays collision-free.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aether.models.base import Base
from aether.models.enums import (
    ConfidenceLevel,
    CreatedByAgent,
    ExpiryPolicy,
    LinkType,
    NodeSource,
    NodeStatus,
    NodeType,
    PillarName,
    pg_enum,
)


class Node(Base):
    """
    TABLE: nodes

    The atomic unit of Aether's memory (Foundation glossary: Memory Node).
    Every piece of information Aether holds is a typed, timestamped,
    confidence-tiered, sourced node.
    """

    __tablename__ = "nodes"
    __table_args__ = (
        CheckConstraint("length(title) <= 120", name="ck_nodes_title_length"),
        Index("idx_nodes_type", "type"),
        Index("idx_nodes_status", "status"),
        Index("idx_nodes_created", "created_at"),
        Index("idx_nodes_metadata", "metadata", postgresql_using="gin"),
        # Functional indexes on JSONB sub-paths (deadline, tags, parties,
        # synthesis_from) and the fulltext-search GIN index are all
        # created directly in Alembic migration 0002 (Section 9), since
        # SQLAlchemy's ORM-level Index() on functional expressions is
        # fragile across dialect versions. Declaring them only once, in
        # the migration, avoids two competing definitions drifting apart.
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    type: Mapped[NodeType] = mapped_column(pg_enum(NodeType, "node_type"), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[NodeSource] = mapped_column(pg_enum(NodeSource, "node_source"), nullable=False)
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        pg_enum(ConfidenceLevel, "confidence_level"), nullable=False
    )
    status: Mapped[NodeStatus] = mapped_column(
        pg_enum(NodeStatus, "node_status"),
        nullable=False,
        server_default=NodeStatus.ACTIVE.value,
    )
    expiry_policy: Mapped[ExpiryPolicy] = mapped_column(
        pg_enum(ExpiryPolicy, "expiry_policy"),
        nullable=False,
        server_default=ExpiryPolicy.PERMANENT.value,
    )
    expiry_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[CreatedByAgent] = mapped_column(
        pg_enum(CreatedByAgent, "created_by_agent"), nullable=False
    )
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="NO ACTION"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # See module docstring: mapped to the literal "metadata" column,
    # Python attribute named metadata_ to avoid colliding with
    # Base.metadata.
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    pillars: Mapped[list["NodePillar"]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )
    links_from: Mapped[list["NodeLink"]] = relationship(
        back_populates="source_node",
        foreign_keys="NodeLink.source_id",
        cascade="all, delete-orphan",
    )
    links_to: Mapped[list["NodeLink"]] = relationship(
        back_populates="target_node",
        foreign_keys="NodeLink.target_id",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Node id={self.id} type={self.type} title={self.title!r} status={self.status}>"


class NodePillar(Base):
    """
    TABLE: node_pillars

    Many-to-many join: one node may belong to one or more pillars, with
    exactly one marked ``is_primary``. Enforcement of "exactly one
    primary pillar per node" is an application-layer rule (write_node()
    step validation), not a DB constraint, since Postgres cannot express
    a per-group uniqueness-of-flag constraint cleanly without a partial
    unique index keyed by node_id — which IS added below.
    """

    __tablename__ = "node_pillars"
    __table_args__ = (
        Index("idx_node_pillars_pillar", "pillar"),
        # At most one primary pillar per node.
        Index(
            "uq_node_pillars_one_primary",
            "node_id",
            unique=True,
            postgresql_where=text("is_primary = true"),
        ),
    )

    node_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pillar: Mapped[PillarName] = mapped_column(
        pg_enum(PillarName, "pillar_name"), primary_key=True, nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    assigned_by: Mapped[CreatedByAgent] = mapped_column(
        pg_enum(CreatedByAgent, "created_by_agent"), nullable=False
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    node: Mapped["Node"] = relationship(back_populates="pillars")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NodePillar node_id={self.node_id} pillar={self.pillar} primary={self.is_primary}>"


class NodeLink(Base):
    """
    TABLE: node_links

    A typed, directed relationship between two nodes. When
    ``link_type == LinkType.CONTRADICTS``, creation of this row must be
    accompanied by the ``contradiction_enforcer`` safety-skill flow
    (INV-07: contradictions are surfaced, never silently resolved).
    """

    __tablename__ = "node_links"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "link_type", name="uq_node_links_triplet"),
        Index("idx_node_links_source", "source_id"),
        Index("idx_node_links_target", "target_id"),
        Index("idx_node_links_type", "link_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    link_type: Mapped[LinkType] = mapped_column(pg_enum(LinkType, "link_type"), nullable=False)
    created_by: Mapped[CreatedByAgent] = mapped_column(
        pg_enum(CreatedByAgent, "created_by_agent"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    source_node: Mapped["Node"] = relationship(back_populates="links_from", foreign_keys=[source_id])
    target_node: Mapped["Node"] = relationship(back_populates="links_to", foreign_keys=[target_id])

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NodeLink {self.source_id} -{self.link_type}-> {self.target_id}>"
