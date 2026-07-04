"""
aether.schemas.session
========================

Pydantic v2 schemas for session state (Phase-0 Prompt Section 6,
``schemas/session.py``).

``L1WorkingMemory`` is Phase-0's entire "brain state" object. It is NOT
a database table — it is a plain Pydantic model that gets serialized
into ``sessions.l1_snapshot`` (JSONB) and rebuilt (or restored) each
session via ``aether/memory/session_state.py``.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from aether.schemas.nodes import DeadlineRef, NodeSummary


class L1WorkingMemory(BaseModel):
    """
    Phase-0 working memory. NOT a DB table.

    Stored as ``sessions.l1_snapshot`` (JSONB). Rebuilt each session by
    ``rebuild_l1()`` unless a valid snapshot already exists (Section 12).
    """

    session_id: str  # UUID as string, for clean JSON round-tripping
    open_tasks: list[NodeSummary] = Field(default_factory=list)
    upcoming_deadlines: list[DeadlineRef] = Field(default_factory=list)
    flagged_nodes: list[NodeSummary] = Field(default_factory=list)
    pending_reviews: int = 0
    last_synthesis_at: str = ""  # ISO 8601, or "" if no synthesis has run
    pillar_summaries: dict[str, str] = Field(default_factory=dict)
    contradiction_count: int = 0


class SessionOpenResponse(BaseModel):
    """Response body for ``POST /session/open``."""

    session_id: UUID
    l1_summary: L1WorkingMemory
