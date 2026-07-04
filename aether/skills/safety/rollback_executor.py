"""
aether.skills.safety.rollback_executor
==========================================

Structural placeholder, not a Phase-0 skill. Section 2's repo
structure lists this file, but Section 1.3's explicit count of "11
total" Phase-0 skills does not include it (3 safety skills are named
there: authority_checker, loop_watchdog, contradiction_enforcer), and
Section 15 gives it no numbered spec of its own. Foundation's glossary
describes it as "the primary mechanism for honoring DP-08
(Reversibility by Default)" — reversing the effects of an *authorized,
executed* action when a correction is required.

There is nothing for this to roll back in Phase-0: the Action Gateway
is a stub that returns ``mock_executed`` and makes no real external
call (Section 1.3, Section 15 SKILL-19), and Phase-0 has no Correction
Loop (explicitly out of scope, Section 1.4). Implementing rollback
logic now would mean inventing behavior for a component with no real
actions to act on and no spec describing what "rollback" should mean
for a mock execution — so this file intentionally contains only the
signature Phase-1 will fill in, not a working implementation.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


async def rollback_executor(action_log_id: UUID, db: AsyncSession) -> None:
    """
    Not implemented in Phase-0. Raises to make that explicit rather
    than silently no-op-ing, since a safety component that appears to
    succeed while doing nothing is worse than one that fails loudly.
    """
    raise NotImplementedError(
        "rollback_executor is not implemented in Phase-0: the Action Gateway "
        "performs no real external actions yet (see aether/skills/operational/"
        "action_gateway.py), so there is nothing to roll back."
    )
