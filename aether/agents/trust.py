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
log) tagged ``trust_maturity``. The live stage is derived by
``current_trust_stage`` — which, as of Phase-2, lives in the ``memory``
layer (``aether.memory.trust_state``) so the ``skills``-layer authority
gates can consume it without a layering violation. It is re-exported
here so existing ``from aether.agents.trust import current_trust_stage``
callers keep working. The gates now read this live value (EC-35),
closing the Phase-1 deferral where they read static config.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.memory.trust_state import TRUST_MARKER, current_trust_stage
from aether.models.enums import (
    ActionType, AgentName, NodeStatus, SessionStatus,
)
from aether.models.logs import ActionLog
from aether.models.nodes import Node
from aether.models.runtime import SkillInvocationLog
from aether.models.sessions import Session

logger = logging.getLogger("aether.agents.trust")

# Canonical live-stage reader now lives in aether.memory.trust_state and
# is re-exported here (see module docstring). ``_MARKER`` is kept as an
# alias of the shared constant so the transition-writer below stays in
# sync with the reader.
_MARKER = TRUST_MARKER
__all__ = ["current_trust_stage", "evaluate_and_advance"]
# Evidence thresholds for "demonstrated reliable L0-L1 operation".
_MIN_CLEAN_SESSIONS = 3
_MIN_OK_INVOCATIONS = 5


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
