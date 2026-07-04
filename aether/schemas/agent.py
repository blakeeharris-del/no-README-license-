"""
aether.schemas.agent
======================

Pydantic v2 schemas for Master Agent I/O (Phase-0 Prompt Section 6,
``schemas/agent.py``).

``UserIntent`` is the structured output of ``cognitive.intent_parser``
and the primary input to ``operational.context_assembler`` (which uses
``implied_pillars`` to select a routing mode — see Section 15 /
``aether/agents/master_agent.py``).

``ApprovalRequest`` is what the Master Agent constructs and returns to
the user for L4 actions; it is NOT what gets sent to the Action
Gateway directly. Per INV-05/INV-10, the agent never calls
``action_gateway()`` itself — only the ``/approve`` endpoint does that,
after the user confirms.
"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class UserIntent(BaseModel):
    """Structured interpretation of a raw user message."""

    raw_input: str
    action_type: Literal["query", "write", "review", "task", "synthesize", "clarify"]
    subject: str
    implied_pillars: list[str] = Field(default_factory=list)
    urgency: Literal["immediate", "standard", "low"]
    time_horizon: Optional[str] = None
    entities: list[str] = Field(default_factory=list)
    ambiguity_flag: bool
    clarification: Optional[str] = None


class ApprovalRequest(BaseModel):
    """
    A plain-language description of a proposed L4 action, presented to
    the user for approval. Constructed by the Master Agent; consumed by
    the ``/approve`` endpoint, which is the only caller of the Action
    Gateway (INV-10).
    """

    action: str
    target: str
    amount_or_consequence: str
    timing: str
    authority_level: int = Field(ge=0, le=5)
    risk_level: Literal["low", "medium", "high", "restricted"]


class AgentResponse(BaseModel):
    """The Master Agent's full response envelope for a single turn."""

    text: str
    nodes_written: list[UUID] = Field(default_factory=list)
    pending_approvals: list[ApprovalRequest] = Field(default_factory=list)
    synthesis_diff: Optional[dict] = None
    flagged_items: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# ContextPacket and its 6 required sections.
#
# Not defined anywhere in Section 6, despite Section 2's repo-structure
# comment listing "agent.py # UserIntent, ContextPacket, AgentResponse,
# etc." — Section 15 (SKILL-15, context_assembler) spells out the exact
# nested-dict shape in prose/comments but no section ever gives it as
# a Pydantic model. Defined here so context_assembler can construct
# and validate it, and so "ContextPacketValidationError if any section
# is missing" (Section 15) has something concrete to validate against
# rather than checking dict keys by hand.
# ---------------------------------------------------------------------------


class ActivePillarSection(BaseModel):
    pillar: str
    routing_mode: Literal["direct", "orchestrated", "synthesis", "direct_write", "challenge_and_prepare"]
    secondary_pillars: list[str] = Field(default_factory=list)


class RelevantNodesSection(BaseModel):
    nodes: list[dict] = Field(default_factory=list)  # NodeSummary dicts
    query_used: str
    total_matched: int
    truncated: bool = False


class InstructionsSection(BaseModel):
    system_prompt: str
    confidence_floor: str
    write_permission: bool
    hard_constraints: list[str] = Field(default_factory=list)
    output_rules: list[str] = Field(default_factory=list)


class OutputFormatSection(BaseModel):
    format_type: str
    required_fields: list[str] = Field(default_factory=list)
    max_length: Optional[int] = None
    confidence_disclosure: bool = False


class ContextPacket(BaseModel):
    """All 6 sections required — see Phase-0 Prompt Section 15."""

    user_intent: dict  # UserIntent dict
    active_pillar: ActivePillarSection
    relevant_nodes: RelevantNodesSection
    session_state: dict  # L1WorkingMemory dict
    instructions: InstructionsSection
    output_format: OutputFormatSection
