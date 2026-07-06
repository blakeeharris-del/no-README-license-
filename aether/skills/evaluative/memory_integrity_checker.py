"""
aether.skills.evaluative.memory_integrity_checker
===================================================

SKILL-29 (Missing Specs). Scans nodes for schema violations (missing
pillar / orphaned, empty synthesis_from, stale flagged, explicit from
non-user), escalates each (p2), and repairs orphaned nodes by assigning
them to the relationships pillar (is_primary=false, auto-flagged).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from aether.models.enums import (
    ActionType, AgentName, ConfidenceLevel, CreatedByAgent, EscalationStatus,
    EscalationType, NodeSource, NodeStatus, PillarName, PriorityClass,
)
from aether.models.logs import ActionLog
from aether.models.nodes import Node, NodePillar
from aether.models.runtime import PendingEscalation

logger = logging.getLogger("aether.skills.evaluative.memory_integrity_checker")


async def check_memory_integrity(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "full_scan": bool}``.
    Returns ``{"nodes_scanned", "violations", "flags_created", "orphaned_nodes"}``.
    """
    session_id = inputs.get("session_id")
    full_scan = bool(inputs.get("full_scan"))
    now = datetime.now(timezone.utc)

    stmt = select(Node)
    if not full_scan and session_id:
        stmt = stmt.where(Node.session_id == uuid.UUID(str(session_id)))
    nodes = list((await db.execute(stmt)).scalars().all())

    # Pillar membership counts, one query.
    pillar_counts = dict((await db.execute(
        select(NodePillar.node_id, func.count()).group_by(NodePillar.node_id)
    )).all())

    violations = []
    flags_created = 0
    orphaned_nodes = 0

    for node in nodes:
        node_violations = []
        if pillar_counts.get(node.id, 0) == 0:
            node_violations.append(("missing_pillar", "Node has no pillar assignment."))
        if node.source == NodeSource.SYNTHESIS and not (node.metadata_ or {}).get("synthesis_from"):
            node_violations.append(("synthesis_from_empty", "Synthesis node missing synthesis_from."))
        if node.confidence == ConfidenceLevel.EXPLICIT and node.created_by != CreatedByAgent.USER:
            node_violations.append(("explicit_from_non_user", "Explicit confidence not from user."))
        if node.status == NodeStatus.FLAGGED and node.updated_at < now - timedelta(days=30):
            node_violations.append(("stale_flagged", "Flagged >30 days with no resolution."))

        for vtype, desc in node_violations:
            violations.append({"node_id": str(node.id), "violation_type": vtype, "description": desc})
            db.add(PendingEscalation(
                escalation_type=EscalationType.CLARIFICATION, priority_class=PriorityClass.P2,
                content={"title": "Memory integrity violation",
                         "description": f"{vtype}: {node.id}", "node_id": str(node.id)},
                session_id=session_id, status=EscalationStatus.PENDING,
            ))
            flags_created += 1

            # Repair orphaned nodes: assign to relationships pillar.
            if vtype == "missing_pillar":
                orphaned_nodes += 1
                db.add(NodePillar(node_id=node.id, pillar=PillarName.RELATIONSHIPS,
                                  is_primary=False, assigned_by=CreatedByAgent.MASTER_AGENT))
                meta = dict(node.metadata_ or {})
                meta["auto_pillar_assigned"] = True
                node.metadata_ = meta
                db.add(ActionLog(
                    session_id=session_id, agent=AgentName.MASTER, action_type=ActionType.SURFACE,
                    node_ids=[node.id],
                    output_summary=f"orphan repair: node {node.id} -> relationships"[:500],
                ))
        if node_violations:
            await db.flush()

    return {
        "nodes_scanned": len(nodes),
        "violations": violations,
        "flags_created": flags_created,
        "orphaned_nodes": orphaned_nodes,
    }
