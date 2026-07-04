"""
aether.skills.safety.authority_checker
=========================================

SKILL-30 (Phase-0 Prompt Section 15). Called DIRECTLY — NOT through
``invoke_skill()`` — since authority checking must be synchronous and
unbypassable; wrapping it in the async invoker would make it just
another skill that could itself be skipped if the invoker were ever
misused. ``check_authority_skill()`` exists only so this same logic is
*also* reachable through the registry for read-only introspection/
testing, per Section 14 listing it in ``SKILL_REGISTRY``.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.enums import ActionType, AgentName
from aether.models.logs import ActionLog

logger = logging.getLogger("aether.skills.safety.authority_checker")

# Authority matrix (Section 15, SKILL-30). action_type -> minimum level.
_MIN_LEVEL_BY_ACTION: dict[str, int] = {
    "read": 0,
    "surface": 1,
    "synthesize": 1,
    "route": 1,
    "write": 2,
    "confirm": 3,
}


def check_authority(agent: str, action_type: str, level: int, db, session_id=None) -> dict:
    """
    Synchronous authority check. Any exception propagates immediately
    — this function does not swallow errors, per the spec's "Cannot be
    bypassed" instruction.

    Deviation flagged for review: Section 15's given signature is
    ``check_authority(agent, action_type, level, db)`` with no
    ``session_id``, but ``action_log.session_id`` is NOT NULL (Section
    5.3) and the spec's own "ON UNAUTHORIZED" instruction says to
    INSERT an ``action_log`` row. Those two requirements are mutually
    exclusive without a session to attach the row to. ``session_id`` is
    added here as an optional keyword argument (every real caller in
    this codebase — ``action_gateway_skill``, ``write_node_skill`` —
    has one available in its own inputs and should pass it); when it is
    genuinely absent, the function still returns the correct
    ``authorized: False`` result but falls back to a Python-logger-only
    record instead of attempting a DB insert that would violate the
    NOT NULL constraint.
    """
    min_level = _MIN_LEVEL_BY_ACTION.get(action_type)
    authorized = min_level is not None and level >= min_level

    # 'confirm' additionally requires trust_stage >= T3.
    if action_type == "confirm" and authorized:
        from aether.config import settings

        trust_rank = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}
        if trust_rank.get(settings.aether_trust_stage, 0) < 3:
            authorized = False

    # synthesis_agent may never confirm, regardless of level/trust stage.
    if agent == AgentName.SYNTHESIS.value and action_type == "confirm":
        authorized = False

    if not authorized:
        violation_summary = f"authority_violation: {agent} attempted {action_type} at L{level}"[:500]
        if session_id is not None:
            db.add(
                ActionLog(
                    session_id=session_id,
                    agent=_safe_agent_name(agent),
                    action_type=ActionType.SURFACE,
                    output_summary=violation_summary,
                )
            )
        else:
            logger.error(
                "[INV-05] Authority violation with no session_id available; "
                "cannot write action_log (NOT NULL constraint) — logged here only. %s",
                violation_summary,
            )
        logger.warning(
            "[INV-05] Authority violation",
            extra={"agent": agent, "action_type": action_type, "level": level},
        )
        return {"authorized": False, "reason": "insufficient_authority"}

    return {"authorized": True, "reason": None}


def _safe_agent_name(agent: str) -> AgentName:
    """
    ``ActionLog.agent`` is a NOT NULL enum column; an authority check
    for an unrecognized agent string must still log something rather
    than raising a second exception while trying to record the first
    violation. Falls back to ``AgentName.MASTER`` for any value that
    isn't a valid ``AgentName`` member.
    """
    try:
        return AgentName(agent)
    except ValueError:
        return AgentName.MASTER


async def check_authority_skill(inputs: dict, db: AsyncSession) -> dict:
    """Async wrapper for invoke_skill/SKILL_REGISTRY compatibility."""
    return check_authority(
        inputs["agent"], inputs["action_type"], inputs["level"], db, session_id=inputs.get("session_id")
    )
