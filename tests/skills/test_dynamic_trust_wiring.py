"""
Dynamic trust-stage wiring into the authority gates (EC-35).

Proves the Phase-2 wiring, not just that it's configured: a
``trust_maturity`` transition logged to ``action_log`` — the live
(dynamic) stage — changes the behavior of both the synchronous
``check_authority`` 'confirm' gate and the ``action_gateway`` execution
gate, WITHOUT touching ``settings.aether_trust_stage``. Before Phase-2
both gates read static config, so the earned stage was inert; these
tests fail against that old behavior.

The behavioral tests request the ``settings_override`` fixture, which
pins ``settings.aether_trust_stage`` to T0, so any behavior change can
only come from the live stage the gates now read — never from static
config.
"""

from __future__ import annotations

import pytest

from aether.memory.trust_state import current_trust_stage
from aether.models.enums import ActionType, AgentName
from aether.models.logs import ActionLog


async def _log_trust_transition(db, session_id, transition: str) -> None:
    """Record an earned trust transition the way agents.trust does, e.g.
    ``"T2->T3"`` -> action_log 'trust_maturity T2->T3: <reason>'."""
    db.add(
        ActionLog(
            session_id=session_id,
            agent=AgentName.MASTER,
            action_type=ActionType.SURFACE,
            # confirmed_by= is required by the 0007 CHECK (sourced advances).
            output_summary=f"trust_maturity {transition}: confirmed_by=test; earned in test",
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_live_stage_defaults_to_config_when_no_transition(db_session, settings_override):
    # No trust_maturity row yet -> falls back to static config (T0).
    assert await current_trust_stage(db_session) == "T0"


@pytest.mark.asyncio
async def test_live_stage_reads_latest_logged_transition(db_session, test_session_row):
    await _log_trust_transition(db_session, test_session_row.id, "T0->T1")
    assert await current_trust_stage(db_session) == "T1"
    await _log_trust_transition(db_session, test_session_row.id, "T2->T3")
    assert await current_trust_stage(db_session) == "T3"


@pytest.mark.asyncio
async def test_confirm_gate_blocks_below_t3_and_authorizes_at_t3(db_session, test_session_row, settings_override):
    """The 'confirm' authority gate is driven by the LIVE stage, not
    static config (which is pinned at T0 the whole time)."""
    from aether.skills.safety.authority_checker import check_authority_skill

    inputs = {
        "agent": "master", "action_type": "confirm", "level": 3,
        "session_id": str(test_session_row.id),
    }

    # Live stage is T0 (no transition) -> confirm blocked despite L3.
    assert (await check_authority_skill(inputs, db_session))["authorized"] is False

    # Earn T3 -> the very same request is now authorized.
    await _log_trust_transition(db_session, test_session_row.id, "T2->T3")
    assert (await check_authority_skill(inputs, db_session))["authorized"] is True


@pytest.mark.asyncio
async def test_gateway_execution_gate_branches_on_live_stage(
    db_session, test_session_row, caplog, settings_override
):
    """action_gateway STEP 4/5 reads the live stage. Both branches return
    mock_executed (no Phase-3 connectors yet), so the only observable
    difference is STEP 5's warning, which fires ONLY when the live stage
    is T2+. Static config stays T0 throughout, so the branch change can
    only come from the earned stage — proving the gate is no longer
    hardwired to static config."""
    import logging

    from aether.skills.operational.action_gateway import action_gateway_skill

    # Approval present so we get past the INV-05 gate to STEP 4.
    db_session.add(
        ActionLog(
            session_id=test_session_row.id, agent=AgentName.MASTER,
            action_type=ActionType.CONFIRM, user_confirmed=True,
        )
    )
    await db_session.commit()

    payload = {
        "action_type": "write", "target": "x", "payload": {}, "authority_level": 3,
        "session_id": str(test_session_row.id), "requesting_agent": "master",
    }
    warn_marker = "no connector routing exists"

    # T0 (live == static): STEP 4 early mock, STEP 5 warning NOT reached.
    with caplog.at_level(logging.WARNING, logger="aether.skills.operational.action_gateway"):
        r0 = await action_gateway_skill(dict(payload), db_session)
    assert r0["status"] == "mock_executed"
    assert warn_marker not in caplog.text

    # Earn T3: STEP 4 no longer short-circuits -> STEP 5 warning fires.
    await _log_trust_transition(db_session, test_session_row.id, "T2->T3")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="aether.skills.operational.action_gateway"):
        r3 = await action_gateway_skill(dict(payload), db_session)
    assert r3["status"] == "mock_executed"
    assert warn_marker in caplog.text
