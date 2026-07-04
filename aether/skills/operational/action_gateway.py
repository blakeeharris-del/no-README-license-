"""
aether.skills.operational.action_gateway
============================================

SKILL-19 (Phase-0 Prompt Section 15). [STUB — Phase-0]. INV-10: the
ONLY path to external execution. Never makes a real call in Phase-0.

NEVER: import httpx, requests, aiohttp, or any external SDK in this
file — enforced by convention here (this is exactly the file INV-10's
CI import-linter rule is watching).
"""

from __future__ import annotations

import logging

from aether.invariants.guards import InvariantViolation, assert_has_user_approval
from aether.models.enums import ActionType, AgentName
from aether.models.logs import ActionLog
from aether.schemas.gateway import GatewayResult

logger = logging.getLogger("aether.skills.operational.action_gateway")


async def action_gateway_skill(inputs: dict, db) -> dict:
    """
    inputs: ``{'action_type', 'target', 'payload', 'authority_level',
    'session_id', 'requesting_agent'}``. Returns a ``GatewayResult`` dict.
    """
    action_type = inputs["action_type"]
    target = inputs["target"]
    authority_level = inputs["authority_level"]
    session_id = inputs["session_id"]
    requesting_agent = inputs["requesting_agent"]

    # STEP 1: log BEFORE any check [INV-01].
    log_entry = ActionLog(
        session_id=session_id,
        agent=AgentName(requesting_agent),
        action_type=ActionType.SURFACE,
        input_summary=f"{action_type} \u2192 {target}"[:500],
    )
    db.add(log_entry)
    await db.flush()
    await db.commit()

    # STEP 2: authority check.
    from aether.skills.safety.authority_checker import check_authority

    authority_result = check_authority(
        requesting_agent, action_type, authority_level, db, session_id=session_id
    )
    if not authority_result["authorized"]:
        return GatewayResult(
            status="blocked", reason="insufficient_authority", log_id=log_entry.id
        ).model_dump(mode="json")

    # STEP 3: user approval [INV-05].
    try:
        await assert_has_user_approval(session_id, db, "action_gateway")
    except InvariantViolation:
        return GatewayResult(
            status="blocked", reason="no_approval", log_id=log_entry.id
        ).model_dump(mode="json")

    # STEP 4: trust-stage gate.
    from aether.config import settings

    if settings.aether_trust_stage in ("T0", "T1"):
        return GatewayResult(
            status="mock_executed",
            mock_response={
                "note": "Phase-0 stub. No external call made.",
                "action_type": action_type,
                "target": target,
            },
            log_id=log_entry.id,
        ).model_dump(mode="json")

    # STEP 5 (Phase-2+): connector routing. NOT implemented in Phase-0.
    logger.warning(
        "action_gateway: trust_stage=%s but no connector routing exists in Phase-0; "
        "returning mock_executed anyway rather than a real call",
        settings.aether_trust_stage,
    )
    return GatewayResult(
        status="mock_executed",
        mock_response={
            "note": "Phase-0 stub. No external call made.",
            "action_type": action_type,
            "target": target,
        },
        log_id=log_entry.id,
    ).model_dump(mode="json")
