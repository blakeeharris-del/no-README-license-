"""
aether.memory.decision_journal
================================

Persistence for the Decision Journal (EC-38). Records each exercised
Decision Protocol run (``executive.decision_protocol``, Foundation §10.6)
and, separately, an explicit *confirmation* that the decision's outcome
was correct.

Layering: this is a ``memory``-layer module — it imports only ``models``.
It reads the protocol's output as a plain dict (no ``skills`` import), so
the caller (which already holds the brief and knows whether the
recommendation was deferred) passes ``deferred`` in.

Confirmation is NEVER synthesized here. ``record_decision`` always writes
``confirmed_correct = NULL`` (unconfirmed). The only way a decision
becomes confirmed is ``confirm_decision``, which requires an explicit
``confirmed_by`` source — the same "explicit act only" discipline as
``/approve`` setting ``user_confirmed`` (Foundation §10.6 / Impl Plan
§18.1: "Only an explicit response … constitutes authorization").
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.logs import DecisionRecord


async def record_decision(
    session_id: UUID,
    proposed_action: str,
    brief: dict,
    deferred: bool,
    db: AsyncSession,
) -> DecisionRecord:
    """Persist one Decision Protocol run. ``brief`` is the skill's output
    dict (sense_summary/analysis/challenge/recommendation/approval_required).
    The record is created UNCONFIRMED — confirmation is a separate act."""
    record = DecisionRecord(
        session_id=session_id,
        proposed_action=proposed_action,
        pillars=list((brief.get("approval_request") or {}).get("target", "").split(", "))
        if brief.get("approval_request") else [],
        sense_summary=brief.get("sense_summary") or "",
        analysis=brief.get("analysis") or "",
        challenge=brief.get("challenge") or "",
        recommendation=brief.get("recommendation") or "",
        deferred=deferred,
        approval_required=bool(brief.get("approval_required")),
        # confirmed_* left NULL — never confirmed at creation time.
    )
    db.add(record)
    await db.flush()
    return record


async def confirm_decision(
    decision_id: UUID,
    confirmed_by: str,
    correct: bool,
    db: AsyncSession,
) -> DecisionRecord:
    """Record an explicit, sourced confirmation of a decision's outcome.

    ``confirmed_by`` is required and must be non-empty — a confirmation
    without a named source is meaningless (and the DB CHECK would reject
    the row anyway). This is the ONLY path that sets ``confirmed_correct``.
    """
    if not confirmed_by or not confirmed_by.strip():
        raise ValueError("confirm_decision requires a non-empty confirmed_by source")

    record = (
        await db.execute(select(DecisionRecord).where(DecisionRecord.id == decision_id))
    ).scalar_one()
    record.confirmed_correct = correct
    record.confirmed_by = confirmed_by
    record.confirmed_at = datetime.now(timezone.utc)
    await db.flush()
    return record
