"""
aether.skills.evaluative.confidence_auditor
==============================================

SKILL-26 (Missing Specs). Reviews inferred/speculative nodes from the
current session (or a synthesis run), verifies each node's confidence
against its evidentiary chain, and flags violations (status=flagged +
p2 escalation + action_log surface entry).

Note: action_log rows are attributed to AgentName.MASTER — the enum has
no dedicated "evaluative" actor, and the master agent is the system
actor on whose behalf evaluative sweeps run.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from aether.models.enums import (
    ActionType, AgentName, ConfidenceLevel, CreatedByAgent, EscalationStatus,
    EscalationType, NodeSource, NodeStatus, PriorityClass,
)
from aether.models.logs import ActionLog
from aether.models.nodes import Node
from aether.models.runtime import PendingEscalation

logger = logging.getLogger("aether.skills.evaluative.confidence_auditor")


def _support_count(node: Node) -> int:
    meta = node.metadata_ or {}
    support = meta.get("synthesis_from") or meta.get("supporting_node_ids") or []
    return len(support)


async def audit_confidence(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "scope": "session"|"synthesis_run",
               "synthesis_run_id": str|null}``.
    Returns ``{"nodes_reviewed", "violations", "flags_created"}``.
    """
    session_id = inputs.get("session_id")
    scope = inputs.get("scope", "session")

    stmt = select(Node).where(
        Node.confidence.in_([ConfidenceLevel.INFERRED, ConfidenceLevel.SPECULATIVE])
    )
    if scope == "synthesis_run":
        stmt = stmt.where(Node.source == NodeSource.SYNTHESIS)
    else:
        stmt = stmt.where(Node.session_id == uuid.UUID(str(session_id)))

    nodes = list((await db.execute(stmt)).scalars().all())

    violations = []
    flags_created = 0
    for node in nodes:
        vtype = None
        warranted = node.confidence.value
        if node.source == NodeSource.SYNTHESIS and _support_count(node) == 0:
            vtype, warranted = "missing_synthesis_from", "speculative"
        elif node.confidence == ConfidenceLevel.INFERRED and _support_count(node) < 3:
            vtype, warranted = "overconfident", "speculative"
        elif node.confidence == ConfidenceLevel.EXPLICIT and node.created_by != CreatedByAgent.USER:
            vtype, warranted = "explicit_from_agent", "inferred"

        if vtype is None:
            continue

        violations.append({
            "node_id": str(node.id),
            "assigned_confidence": node.confidence.value,
            "warranted_confidence": warranted,
            "violation_type": vtype,
        })

        # Actions: flag node, escalate (p2), log.
        node.status = NodeStatus.FLAGGED
        db.add(PendingEscalation(
            escalation_type=EscalationType.CLARIFICATION,
            priority_class=PriorityClass.P2,
            content={"title": "Confidence audit finding",
                     "description": f"{vtype} on node {node.id}", "node_id": str(node.id)},
            session_id=session_id, status=EscalationStatus.PENDING,
        ))
        db.add(ActionLog(
            session_id=session_id, agent=AgentName.MASTER, action_type=ActionType.SURFACE,
            node_ids=[node.id], output_summary=f"confidence audit: {vtype} on {node.id}"[:500],
        ))
        await db.flush()
        flags_created += 1

    return {"nodes_reviewed": len(nodes), "violations": violations, "flags_created": flags_created}
