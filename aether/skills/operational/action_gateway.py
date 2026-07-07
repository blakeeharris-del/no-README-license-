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

    # Resolve the LIVE trust stage once (EC-35) and use it for both the
    # authority 'confirm' gate (STEP 2) and the execution gate (STEP 4).
    # Previously both read settings.aether_trust_stage (static config),
    # leaving the earned stage logged-but-inert.
    from aether.memory.trust_state import current_trust_stage

    trust_stage = await current_trust_stage(db)

    # STEP 2: authority check.
    from aether.skills.safety.authority_checker import check_authority

    authority_result = check_authority(
        requesting_agent, action_type, authority_level, db,
        session_id=session_id, trust_stage=trust_stage,
    )
    if not authority_result["authorized"]:
        return GatewayResult(
            status="blocked", reason="insufficient_authority", log_id=log_entry.id
        ).model_dump(mode="json")

    # STEP 3: authorization [INV-05].
    #
    # Standing authority (EC-36, Foundation §9.2 T3) is a NARROW,
    # all-conditions-must-hold bypass of the *per-action* approval step —
    # NOT a bypass of authorization or of logging. INV-05 (line 227, "no
    # exception, no bypass, no implicit authorization") is preserved: a
    # standing grant IS the logged, user-approved authorization (explicit,
    # Blake-authored, permanent), and §9.2 removes only the per-action
    # confirmation. Every execution under a grant is logged here — the
    # permanent per-execution record INV-05 requires. (See the INV-05-vs-§9.2
    # reconciliation flagged in HANDOFF_PHASE2.md.)
    #
    # Anything short of the full conjunction (active grant covering
    # (action_type, pillar) AND live trust >= T3 AND within bounds AND
    # before renewal) falls through to the unchanged per-action approval.
    from aether.memory.standing_authority import find_valid_standing_grant

    # ``action_name`` is the SPECIFIC external action (e.g.
    # 'categorize_transaction') that a standing grant scopes — distinct from
    # the abstract ``action_type`` (read/write/confirm) the authority matrix
    # checks at STEP 2. Absent ``action_name`` (the ordinary case), no grant
    # matches and the per-action approval default applies.
    pillar = inputs.get("pillar")
    action_name = inputs.get("action_name")
    grant = await find_valid_standing_grant(
        action_name, pillar, trust_stage, inputs.get("payload") or {}, db
    )
    if grant is not None:
        db.add(ActionLog(
            session_id=session_id,
            agent=AgentName(requesting_agent),
            action_type=ActionType.SURFACE,
            output_summary=f"executed under standing_authority {grant.id} ({pillar}/{action_name})"[:500],
        ))
        await db.flush()
    else:
        # Default path (UNCHANGED): per-action approval required [INV-05].
        try:
            await assert_has_user_approval(session_id, db, "action_gateway")
        except InvariantViolation:
            return GatewayResult(
                status="blocked", reason="no_approval", log_id=log_entry.id
            ).model_dump(mode="json")

    # STEP 4: trust-stage gate. Reads the LIVE stage (EC-35), not static
    # config: below T2, execution is always mocked.
    if trust_stage in ("T0", "T1"):
        return GatewayResult(
            status="mock_executed",
            mock_response={
                "note": "Phase-0 stub. No external call made.",
                "action_type": action_type,
                "target": target,
            },
            log_id=log_entry.id,
        ).model_dump(mode="json")

    # STEP 5 (Phase-3+): connector routing. Still NOT implemented — live
    # external execution is out of scope until Phase 3. Even at T2+ the
    # gateway returns mock_executed; what changed in Phase-2 is that the
    # gate now branches on the *live* trust stage, so a genuine T2/T3
    # advance reaches this path instead of being short-circuited above.
    logger.warning(
        "action_gateway: trust_stage=%s but no connector routing exists yet; "
        "returning mock_executed anyway rather than a real call",
        trust_stage,
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
