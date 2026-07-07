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

from sqlalchemy.ext.asyncio import AsyncSession

from aether.memory.trust_state import (
    _LADDER,
    TRUST_MARKER,
    current_trust_stage,
    execute_trust_advance,
    surface_advancement_evidence,
)

logger = logging.getLogger("aether.agents.trust")

# Canonical live-stage reader + the advancement ladder now live in
# aether.memory.trust_state (memory layer, so the skills-layer gate can
# consume them). Re-exported here for existing callers.
_MARKER = TRUST_MARKER
__all__ = [
    "current_trust_stage", "evaluate_and_advance",
    "surface_advancement_evidence", "execute_trust_advance",
]


async def evaluate_and_advance(
    db: AsyncSession, session_id: UUID, confirmed_by: str | None = None
) -> str:
    """
    Sign-off-gated advancement (Blake's ruling: NO stage advances
    automatically, T0->T1 included — this is the intended surgery on the
    Phase-1 auto-advancing version).

    Without ``confirmed_by`` this SURFACES only: it advances nothing and
    returns the live stage. That is AETHER's role — present the evidence,
    never self-promote. With an explicit ``confirmed_by`` (Blake's role) it
    executes the single next ladder step via ``execute_trust_advance``,
    which still requires the evidence bar to be met. There is no code path
    that advances the stage without an ``execute_trust_advance`` call.
    """
    current = await current_trust_stage(db)
    next_stage = _LADDER.get(current)
    if next_stage is None:
        return current  # already at the ceiling the ladder reaches (T3)
    if confirmed_by is None:
        return current  # surface-only; no advance without an explicit sign-off
    return await execute_trust_advance(current, next_stage, confirmed_by, session_id, db)
