"""
aether.agents.trust
=====================

Trust maturity (Foundation §9.2). Trust stages T0-T4 are *earned on
evidence*, not set at configuration time (Foundation §9.2, §authority
matrix). Phase-1's bar (EC-25) is the first earned transition:

  T0 (Setup — no track record)
    -> T1 (demonstrated reliable L0-L1 operation)

with the transition itself logged.

Storage: rather than add a table outside the Phase-1 five-table scope,
each transition is recorded as an ``action_log`` row (the system audit
log) tagged ``trust_maturity``. ``current_trust_stage`` derives the live
stage from the most recent such row, falling back to the static config
default (T0). Wiring the *dynamic* stage into the action_gateway /
authority_checker gates (which still read the static config) is a
Phase-2+ refinement — Phase-1 adds no live external actions, so the
gate value is not exercised.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.config import settings
from aether.models.enums import (
    ActionType, AgentName, NodeStatus, SessionStatus,
)
from aether.models.logs import ActionLog
from aether.models.nodes import Node
from aether.models.runtime import SkillInvocationLog
from aether.models.sessions import Session

logger = logging.getLogger("aether.agents.trust")

_MARKER = "trust_maturity"
# Evidence thresholds for "demonstrated reliable L0-L1 operation".
_MIN_CLEAN_SESSIONS = 3
_MIN_OK_INVOCATIONS = 5


async def current_trust_stage(db: AsyncSession) -> str:
    """Live trust stage: latest logged transition, else config default."""
    row = (
        await db.execute(
            select(ActionLog.output_summary)
            .where(ActionLog.output_summary.like(f"{_MARKER}%"))
            .order_by(ActionLog.timestamp.desc())
            .limit(1)
        )
    ).first()
    if row and row[0] and "->" in row[0]:
        # format: "trust_maturity T0->T1: <reason>"
        try:
            return row[0].split("->", 1)[1].split(":", 1)[0].strip()
        except IndexError:
            pass
    return settings.aether_trust_stage


async def _reliable_l0_l1_evidence(db: AsyncSession) -> tuple[bool, str]:
    closed_sessions = (
        await db.execute(
            select(func.count()).select_from(Session).where(Session.status == SessionStatus.CLOSED)
        )
    ).scalar_one()
    ok_invocations = (
        await db.execute(
            select(func.count()).select_from(SkillInvocationLog)
            .where(SkillInvocationLog.status == "ok")
        )
    ).scalar_one()
    ok = closed_sessions >= _MIN_CLEAN_SESSIONS and ok_invocations >= _MIN_OK_INVOCATIONS
    reason = (f"{closed_sessions} closed sessions, {ok_invocations} successful "
              f"L0-L1 skill invocations")
    return ok, reason


async def evaluate_and_advance(db: AsyncSession, session_id: UUID) -> str:
    """
    Advance T0 -> T1 if reliable L0-L1 operation is demonstrated, logging
    the transition. Returns the (possibly advanced) current stage.
    """
    current = await current_trust_stage(db)
    if current != "T0":
        return current  # Phase-1 only handles the first earned transition

    ok, reason = await _reliable_l0_l1_evidence(db)
    if not ok:
        return "T0"

    db.add(ActionLog(
        session_id=session_id,
        agent=AgentName.MASTER,
        action_type=ActionType.SURFACE,
        output_summary=f"{_MARKER} T0->T1: {reason}"[:500],
    ))
    await db.flush()
    logger.info("Trust maturity advanced T0->T1 (%s)", reason)
    return "T1"
