"""
aether.skills.safety.contradiction_enforcer
===============================================

Called directly from ``write_node()`` and (in Phase-1) ``node_linker``
— NOT through ``invoke_skill()``. ATOMIC: all steps in one transaction
or none. INV-07: detects -> flags -> escalates. NEVER resolves.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.enums import (
    ActionType,
    AgentName,
    CreatedByAgent,
    EscalationStatus,
    EscalationType,
    LinkType,
    NodeStatus,
    PriorityClass,
)
from aether.models.logs import ActionLog
from aether.models.nodes import Node, NodeLink
from aether.models.runtime import PendingEscalation

logger = logging.getLogger("aether.skills.safety.contradiction_enforcer")


async def contradiction_enforcer(
    node_a_id: UUID,
    node_b_id: UUID,
    session_id: UUID,
    db: AsyncSession,
) -> None:
    """
    Flags both nodes, links them as contradicting, and escalates.
    Never chooses a winner, never archives/supersedes either node,
    never modifies node content.
    """
    # ---- 1: flag both nodes -------------------------------------------
    # Direct ORM update — deliberately NOT write_node(), to avoid
    # recursion (write_node() itself is what may have called this).
    await db.execute(
        update(Node).where(Node.id.in_([node_a_id, node_b_id])).values(status=NodeStatus.FLAGGED)
    )

    # ---- 2: link, tolerating a pre-existing identical link -------------
    try:
        async with db.begin_nested():
            db.add(
                NodeLink(
                    source_id=node_a_id,
                    target_id=node_b_id,
                    link_type=LinkType.CONTRADICTS,
                    created_by=CreatedByAgent.MASTER_AGENT,
                )
            )
    except IntegrityError:
        logger.info(
            "Contradiction link already exists; skipping duplicate",
            extra={"node_a": str(node_a_id), "node_b": str(node_b_id)},
        )

    # ---- 3: escalate -----------------------------------------------------
    db.add(
        PendingEscalation(
            escalation_type=EscalationType.P0_SIGNAL,
            priority_class=PriorityClass.P1,
            content={
                "title": "Contradiction detected",
                "description": f"Nodes {node_a_id} and {node_b_id} contradict.",
                "node_id": str(node_a_id),
            },
            session_id=session_id,
            status=EscalationStatus.PENDING,
        )
    )

    # ---- 4: log [INV-01] ---------------------------------------------------
    db.add(
        ActionLog(
            session_id=session_id,
            agent=AgentName.MASTER,
            action_type=ActionType.SURFACE,
            node_ids=[node_a_id, node_b_id],
            output_summary=f"contradiction: {node_a_id} vs {node_b_id}"[:500],
        )
    )

    # ---- 5: commit -----------------------------------------------------------
    await db.commit()
