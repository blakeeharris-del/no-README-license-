"""
aether.skills.operational.deadline_monitor
=============================================

SKILL-17 (Missing Specs). Scans active nodes with deadline metadata
across all pillars (or a given subset), scores each via
cognitive.signal_scorer, and inserts pending_escalations for P0/P1
items — deduplicated against existing pending escalations for the same
node (spec: check content->>'node_id' before inserting).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from aether.memory.read_protocol import read_by_deadline
from aether.models.enums import EscalationStatus, EscalationType, PillarName, PriorityClass
from aether.models.nodes import Node
from aether.models.runtime import PendingEscalation
from aether.skills.cognitive.signal_scorer import score_signal

logger = logging.getLogger("aether.skills.operational.deadline_monitor")

_ALL_PILLARS = [p.value for p in PillarName]
_ESCALATE = {PriorityClass.P0.value, PriorityClass.P1.value}


async def _has_pending_escalation(db, session_id, node_id: str) -> bool:
    existing = (
        await db.execute(
            select(PendingEscalation.id).where(
                PendingEscalation.status == EscalationStatus.PENDING,
                PendingEscalation.content["node_id"].astext == node_id,
            )
        )
    ).first()
    return existing is not None


async def monitor_deadlines(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "pillars": [str]|null}``.
    Returns ``{"deadlines_found", "p0_count", "p1_count",
    "escalations_created", "deadline_list"}``.
    """
    session_id = inputs.get("session_id")
    pillars = inputs.get("pillars") or _ALL_PILLARS

    now = datetime.now(timezone.utc)
    refs = await read_by_deadline(pillars, now - timedelta(days=30), now + timedelta(days=90), db)

    if not refs:  # FM-01
        return {"deadlines_found": 0, "p0_count": 0, "p1_count": 0,
                "escalations_created": 0, "deadline_list": []}

    nodes_by_id = {
        n.id: n
        for n in (await db.execute(
            select(Node).where(Node.id.in_([r.node_id for r in refs]))
        )).scalars().all()
    }

    deadline_list = []
    p0_count = p1_count = escalations_created = 0

    for ref in refs:
        node = nodes_by_id.get(ref.node_id)
        amount = (node.metadata_ or {}).get("amount") if node else None
        if ref.days_until < 0:
            priority_class = PriorityClass.P0.value
        else:
            try:
                scored = await score_signal(
                    {"signal": {"type": "deadline", "pillar": ref.pillar.value,
                                "source": (node.source.value if node else None),
                                "days_until": ref.days_until, "amount": amount}},
                    db,
                )
                priority_class = scored["priority_class"]
            except Exception:  # FM-02: scorer failure -> p3, continue
                priority_class = PriorityClass.P3.value

        if priority_class == PriorityClass.P0.value:
            p0_count += 1
        elif priority_class == PriorityClass.P1.value:
            p1_count += 1

        deadline_list.append({
            "node_id": str(ref.node_id), "title": ref.title, "deadline": ref.deadline,
            "days_until": ref.days_until, "pillar": ref.pillar.value,
            "priority_class": priority_class,
        })

        if priority_class in _ESCALATE and session_id:
            if not await _has_pending_escalation(db, session_id, str(ref.node_id)):
                db.add(PendingEscalation(
                    escalation_type=EscalationType.P0_SIGNAL,
                    priority_class=PriorityClass(priority_class),
                    content={"title": f"Deadline: {ref.title}",
                             "description": f"Due {ref.deadline} ({ref.days_until} days).",
                             "node_id": str(ref.node_id)},
                    session_id=session_id,
                    status=EscalationStatus.PENDING,
                ))
                await db.flush()
                escalations_created += 1

    return {
        "deadlines_found": len(deadline_list),
        "p0_count": p0_count,
        "p1_count": p1_count,
        "escalations_created": escalations_created,
        "deadline_list": deadline_list,
    }
