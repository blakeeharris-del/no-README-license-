"""
aether.skills.safety.rollback_executor
==========================================

The real Phase-1 implementation, replacing Phase-0's NotImplementedError
placeholder (EC-27). The primary mechanism for honoring DP-08
(Reversibility by Default, Foundation glossary): reverses the effect of
an authorized, executed action when a correction is required.

Safety-skill rules (Missing Specs): called directly (NOT via
invoke_skill), never hard-deletes (INV-02 absolute), always inserts a
pending_escalation notifying the user.

Steps (Missing Specs ROLLBACK RULES):
  1. node.status = 'archived'  (never DELETE — INV-02)
  2. action_log: action_type='surface', output_summary describes rollback
  3. pending_escalation: clarification / p2, notifying the user
  4. node_links are left intact (only the node status changes)
  5. if this node superseded another (supersedes link source=node_id),
     restore that superseded node to 'active'
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aether.invariants.guards import NodeNotFoundError
from aether.models.enums import (
    ActionType, AgentName, EscalationStatus, EscalationType, LinkType, NodeStatus, PriorityClass,
)
from aether.models.logs import ActionLog
from aether.models.nodes import Node, NodeLink
from aether.models.runtime import PendingEscalation

logger = logging.getLogger("aether.skills.safety.rollback_executor")


async def rollback_executor(
    node_id: UUID,
    db: AsyncSession,
    *,
    session_id: UUID | None = None,
    reason: str = "correction required",
) -> None:
    """
    Archive ``node_id`` (never delete), notify the user, and restore any
    node it superseded. Called directly as a synchronous safety flow;
    exceptions propagate (they are never swallowed).
    """
    node = (await db.execute(select(Node).where(Node.id == node_id))).scalar_one_or_none()
    if node is None:
        raise NodeNotFoundError(node_id)

    # 1. Archive (never delete — INV-02).
    node.status = NodeStatus.ARCHIVED

    # 5. Restore any node this one superseded (supersedes: source -> target).
    superseded_targets = (await db.execute(
        select(NodeLink.target_id).where(
            NodeLink.source_id == node_id, NodeLink.link_type == LinkType.SUPERSEDES
        )
    )).scalars().all()
    for target_id in superseded_targets:
        await db.execute(
            update(Node).where(Node.id == target_id, Node.status == NodeStatus.SUPERSEDED)
            .values(status=NodeStatus.ACTIVE)
        )

    # 2. Log to action_log (surface). session_id is NOT NULL on action_log,
    # so this requires a session context; if a caller invokes rollback with
    # no session (rare), the escalation below still notifies the user.
    if session_id is not None:
        db.add(ActionLog(
            session_id=session_id,
            agent=AgentName.MASTER,
            action_type=ActionType.SURFACE,
            node_ids=[node_id],
            output_summary=f"rollback: node {node_id} archived due to {reason}"[:500],
        ))

    # 3. Notify the user via escalation.
    db.add(PendingEscalation(
        escalation_type=EscalationType.CLARIFICATION,
        priority_class=PriorityClass.P2,
        content={"title": "Node rolled back", "description": reason, "node_id": str(node_id)},
        session_id=session_id,
        status=EscalationStatus.PENDING,
    ))

    await db.flush()
    logger.info("rollback_executor: archived node %s (%s)", node_id, reason)
