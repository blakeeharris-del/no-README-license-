"""
aether.skills.analytical.legal_deadline_surfacer
===================================================

SKILL-08 (Missing Specs). Surfaces legal-pillar deadlines within a
look-ahead window, priority-scored via cognitive.signal_scorer, and
escalates P0/P1 items.

Authority nuance: the spec labels this L0 "read-only", which refers to
memory-node authority — this skill never writes or mutates L2/L3
nodes. Inserting ``pending_escalations`` rows (an operational surface,
which aether_app_role holds INSERT on) is the explicit behavior the
spec's Implementation Notes require ("Insert escalations for P0/P1
items"), not a memory write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from aether.memory.read_protocol import read_by_deadline
from aether.models.enums import EscalationStatus, EscalationType, PriorityClass
from aether.models.nodes import Node
from aether.models.runtime import PendingEscalation
from aether.skills.cognitive.signal_scorer import score_signal

logger = logging.getLogger("aether.skills.analytical.legal_deadline_surfacer")

_ESCALATE = {PriorityClass.P0.value, PriorityClass.P1.value}


async def surface_legal_deadlines(inputs: dict, db) -> dict:
    """
    inputs: ``{"days_ahead": int (default 90), "session_id": str}``.
    Returns ``{"deadlines": [...], "total_found": int, "overdue_count": int}``,
    sorted by days_until ASC, and inserts P0/P1 escalations as a side effect.
    """
    days_ahead = int(inputs.get("days_ahead") or 90)
    session_id = inputs.get("session_id")

    now = datetime.now(timezone.utc)
    refs = await read_by_deadline(["legal"], now, now + timedelta(days=days_ahead), db)

    if not refs:  # FM-01
        return {"deadlines": [], "total_found": 0, "overdue_count": 0}

    # Fetch the underlying nodes for confidence + obligation_type metadata.
    node_ids = [r.node_id for r in refs]
    nodes_by_id = {
        n.id: n
        for n in (await db.execute(select(Node).where(Node.id.in_(node_ids)))).scalars().all()
    }

    deadlines = []
    overdue_count = 0
    for ref in refs:
        node = nodes_by_id.get(ref.node_id)
        confidence = node.confidence.value if node else "speculative"
        obligation_type = (node.metadata_ or {}).get("obligation_type") if node else None
        amount = (node.metadata_ or {}).get("amount") if node else None

        # Priority via signal_scorer; overdue is always P0.
        if ref.days_until < 0:
            priority_class = PriorityClass.P0.value
        else:
            scored = await score_signal(
                {"signal": {
                    "type": "deadline", "pillar": "legal",
                    "source": (node.source.value if node else None),
                    "days_until": ref.days_until, "amount": amount,
                }},
                db,
            )
            priority_class = scored["priority_class"]

        if ref.days_until < 0:
            overdue_count += 1

        deadlines.append({
            "node_id": str(ref.node_id),
            "title": ref.title,
            "deadline": ref.deadline,
            "days_until": ref.days_until,
            "pillar": "legal",
            "confidence": confidence,
            "priority_class": priority_class,
            "obligation_type": obligation_type,
        })

        if priority_class in _ESCALATE and session_id:
            await _escalate(db, session_id, ref, priority_class)

    deadlines.sort(key=lambda d: d["days_until"])
    return {
        "deadlines": deadlines,
        "total_found": len(deadlines),
        "overdue_count": overdue_count,
    }


async def _escalate(db, session_id, ref, priority_class: str) -> None:
    """Insert a pending escalation, guarded against duplicate pending P0
    rows (the pending_escalations partial-unique index) via a savepoint."""
    escalation = PendingEscalation(
        escalation_type=EscalationType.P0_SIGNAL,
        priority_class=PriorityClass(priority_class),
        content={
            "title": f"Legal deadline: {ref.title}",
            "description": f"Due {ref.deadline} ({ref.days_until} days).",
            "node_id": str(ref.node_id),
        },
        session_id=session_id,
        status=EscalationStatus.PENDING,
    )
    try:
        async with db.begin_nested():
            db.add(escalation)
            await db.flush()
    except Exception:
        # Duplicate pending P0 for this node/session already exists — fine.
        logger.debug("escalation for node %s already pending; skipped", ref.node_id)
