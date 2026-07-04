"""
aether.memory.session_state
==============================

``rebuild_l1()`` and ``save_l1_snapshot()`` (Phase-0 Prompt Section 12).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.memory.read_protocol import _to_node_summaries, fetch_l3, read_by_deadline, read_by_type
from aether.models.enums import LinkType, NodeStatus, NodeType, PillarName
from aether.models.logs import SynthesisRun
from aether.models.nodes import Node, NodeLink
from aether.models.sessions import Session
from aether.schemas.session import L1WorkingMemory

logger = logging.getLogger("aether.memory.session_state")

_ALL_PILLARS = list(PillarName)


async def rebuild_l1(session_id: UUID, db: AsyncSession) -> L1WorkingMemory:
    """
    Rebuild ``L1WorkingMemory`` from L2/L3, or restore it directly from
    ``sessions.l1_snapshot`` if a valid one already exists.

    Failure cases are all valid, non-error outcomes (Section 12): a new
    user with no nodes yields an all-empty L1; a corrupt snapshot logs
    a WARNING and falls through to a full rebuild rather than raising.
    """
    # ---- STEP 1: try the existing snapshot first -----------------------
    session_row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()
    if session_row.l1_snapshot is not None:
        try:
            return L1WorkingMemory.model_validate(session_row.l1_snapshot)
        except ValidationError:
            logger.warning(
                "Corrupt L1 snapshot for session %s; rebuilding from scratch", session_id
            )

    # ---- STEP 2: L3 pillar summaries -----------------------------------
    pillar_summaries: dict[str, str] = {}
    for pillar in _ALL_PILLARS:
        l3_nodes = await fetch_l3(pillar, db, limit=5)
        pillar_summaries[pillar.value] = "; ".join(n.title for n in l3_nodes) if l3_nodes else ""

    # ---- STEP 3: upcoming deadlines (next 30 days) ----------------------
    now = datetime.now(timezone.utc)
    deadlines = await read_by_deadline(_ALL_PILLARS, now, now + timedelta(days=30), db)
    deadlines = sorted(deadlines, key=lambda d: d.days_until)[:20]

    # ---- STEP 4: open tasks ---------------------------------------------
    open_tasks = await read_by_type(NodeType.TASK, _ALL_PILLARS, db)
    open_tasks = [t for t in open_tasks if t.status == NodeStatus.ACTIVE][:15]

    # ---- STEP 5: flagged nodes ------------------------------------------
    flagged_stmt = (
        select(Node).where(Node.status == NodeStatus.FLAGGED).order_by(Node.updated_at.desc()).limit(10)
    )
    flagged_rows = (await db.execute(flagged_stmt)).scalars().all()
    flagged_nodes = await _to_node_summaries(list(flagged_rows), db)

    # ---- STEP 6: pending review count ------------------------------------
    pending_reviews = (
        await db.execute(
            select(func.count()).select_from(Node).where(Node.status == NodeStatus.PENDING_REVIEW)
        )
    ).scalar_one()

    # ---- STEP 7: last synthesis timestamp --------------------------------
    last_synthesis_row = (
        await db.execute(
            select(SynthesisRun.completed_at)
            .where(SynthesisRun.completed_at.isnot(None))
            .order_by(SynthesisRun.completed_at.desc())
            .limit(1)
        )
    ).first()
    last_synthesis_at = last_synthesis_row[0].isoformat() if last_synthesis_row else ""

    # ---- STEP 8: contradiction count ---------------------------------------
    contradiction_count = (
        await db.execute(
            select(func.count(func.distinct(NodeLink.source_id))).where(
                NodeLink.link_type == LinkType.CONTRADICTS
            )
        )
    ).scalar_one()

    # ---- STEP 9: build + persist ------------------------------------------
    l1 = L1WorkingMemory(
        session_id=str(session_id),
        open_tasks=open_tasks,
        upcoming_deadlines=deadlines,
        flagged_nodes=flagged_nodes,
        pending_reviews=pending_reviews,
        last_synthesis_at=last_synthesis_at,
        pillar_summaries=pillar_summaries,
        contradiction_count=contradiction_count,
    )
    await save_l1_snapshot(session_id, l1, db)

    # ---- STEP 10 ------------------------------------------------------------
    return l1


async def save_l1_snapshot(session_id: UUID, l1: L1WorkingMemory, db: AsyncSession) -> None:
    """
    Serialize ``L1WorkingMemory`` to JSON-compatible data and persist it
    to ``sessions.l1_snapshot``. ``model_dump(mode="json")`` converts
    every UUID (and datetime) to its string form, per Section 12's
    explicit instruction to convert UUIDs to str before serialization.
    """
    snapshot = l1.model_dump(mode="json")
    session_row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()
    session_row.l1_snapshot = snapshot
