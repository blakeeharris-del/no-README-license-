"""
aether.schemas.nodes
======================

Pydantic v2 schemas for node read/write operations (Phase-0 Prompt
Section 6, ``schemas/nodes.py``).

These are the DTOs that cross the boundary between skills/agents and
the memory layer. ``NodeDraft`` is what a caller proposes to
``write_node()``; ``NodeSummary`` is the read-side projection used
throughout context assembly and L1 working memory.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aether.models.enums import (
    ConfidenceLevel,
    CreatedByAgent,
    ExpiryPolicy,
    NodeSource,
    NodeStatus,
    NodeType,
    PillarName,
)


class NodeDraft(BaseModel):
    """A proposed node, not yet written. Input to ``write_node()``."""

    type: NodeType
    title: str = Field(max_length=120)
    content: str
    source: NodeSource
    confidence: ConfidenceLevel
    created_by: CreatedByAgent
    pillars: list[PillarName] = Field(min_length=1)
    primary_pillar: PillarName
    expiry_policy: ExpiryPolicy = ExpiryPolicy.PERMANENT
    expiry_date: Optional[datetime] = None
    metadata: dict = Field(default_factory=dict)


class NodeSummary(BaseModel):
    """Read-side projection of a node. ORM-mapped via ``from_attributes``."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: NodeType
    title: str
    content: str
    confidence: ConfidenceLevel
    status: NodeStatus
    pillars: list[PillarName]
    deadline: Optional[str] = None
    created_at: datetime
    link_count: int = 0


class WriteResult(BaseModel):
    """Result of a successful ``write_node()`` call."""

    node_id: UUID
    status: Literal["written", "written_with_contradiction"]
    contradiction_node_ids: list[UUID] = Field(default_factory=list)


class DeadlineRef(BaseModel):
    """A single upcoming (or overdue) deadline, as surfaced by read_protocol."""

    node_id: UUID
    title: str
    deadline: str
    pillar: PillarName
    days_until: int


class ConflictPair(BaseModel):
    """One detected contradiction between a candidate node and an existing node."""

    node_id: UUID
    existing_title: str
    conflict_description: str
    conflict_severity: Literal["direct", "partial", "temporal"]


class ConflictResult(BaseModel):
    """Output of ``cognitive.contradiction_detector``."""

    conflicts: list[ConflictPair] = Field(default_factory=list)
