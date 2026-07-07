"""
tests.loops.test_safety_loop — Safety Loop (Impl Plan §16.4 / Missing
Specs Vol 3 LOOP-04).

Safety is a per-trigger dispatcher, not an iterating loop: max_iterations
is null by design (LOOP-04 line 2768), so there is NO iteration bound to
force. EC-29 for Safety is proven by the DURATION bound — forcing a
response past the 10,000ms limit and confirming the fail-safe
force-terminates the triggering process and drives the
loop_runs(loop_type='safety') row to terminal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from aether.loops.safety_loop import SafetyLoop, SafetyRiskType
from aether.models.enums import (
    EscalationStatus,
    EscalationType,
    LoopStatus,
    LoopType,
    PriorityClass,
    SubAgentStatus,
)
from aether.models.runtime import LoopRun, PendingEscalation, SubAgentRun


async def _running_loop(db, session_id, loop_type=LoopType.GOAL, **kw):
    row = LoopRun(
        loop_type=loop_type, trigger="test", session_id=session_id,
        status=LoopStatus.RUNNING, iteration_count=1,
        max_iterations=kw.get("max_iterations", 10), max_duration_ms=kw.get("max_duration_ms", 120000),
        start_time=kw.get("start_time"),
    )
    db.add(row)
    await db.commit()
    return row


# ---------------------------------------------------------------------------
# LOOP-04 logging: every trigger produces a bounded safety loop_runs row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_trigger_produces_safety_loop_run(db_session, test_session_row):
    outcome = await SafetyLoop().run(
        SafetyRiskType.AUTHORITY,
        {"detail": "master attempted confirm at L2"},
        test_session_row.id, db_session,
    )
    row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.safety_loop_run_id)
    )).scalar_one()
    assert row.loop_type == LoopType.SAFETY
    assert row.trigger == "authority"
    # config-vs-schema reconciliation: placeholder max_iterations=1, real
    # bound is the 10s duration.
    assert row.max_iterations == 1
    assert row.max_duration_ms == 10000
    assert row.status == LoopStatus.COMPLETED
    assert outcome.risk_neutralized is True


# ---------------------------------------------------------------------------
# EC-29 for Safety, proven by force: the DURATION bound + fail-safe
# ---------------------------------------------------------------------------


async def _slow_response(loop_run, db):
    # Simulate a response that took > 10s by back-dating the safety
    # loop_run's own start_time (deterministic; no real sleep).
    loop_run.start_time = datetime.now(timezone.utc) - timedelta(
        milliseconds=SafetyLoop.MAX_RESPONSE_MS + 3000
    )
    await db.flush()


@pytest.mark.asyncio
async def test_ec29_duration_failsafe_force_terminates_triggering_process(db_session, test_session_row):
    """Force a response past 10,000ms: the fail-safe must force-terminate
    the triggering loop and drive the safety loop_runs row to terminal
    (LOOP-04 lines 2828-2832). Uses AUTHORITY (a P1 response, no prior P0)
    so the fail-safe's P0 safety_timeout survives as a distinct row."""
    triggering = await _running_loop(db_session, test_session_row.id)

    outcome = await SafetyLoop().run(
        SafetyRiskType.AUTHORITY,
        {"triggering_loop_run_id": triggering.id, "detail": "master attempted confirm at L2"},
        test_session_row.id, db_session,
        response_hook=_slow_response,
    )

    assert outcome.fail_safe_triggered is True
    assert outcome.risk_neutralized is False
    assert outcome.terminated_process_id == triggering.id

    # Real row states, asserted against Postgres:
    safety_row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.safety_loop_run_id)
    )).scalar_one()
    assert safety_row.status == LoopStatus.FORCED_TERMINATION
    assert safety_row.end_time is not None

    trig_after = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == triggering.id)
    )).scalar_one()
    assert trig_after.status == LoopStatus.FORCED_TERMINATION   # triggering process stopped
    assert trig_after.end_time is not None

    # p0 safety_timeout escalation queued (distinct — no prior P0).
    esc = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.session_id == test_session_row.id,
            PendingEscalation.priority_class == PriorityClass.P0,
        )
    )).scalars().all()
    assert any(e.content.get("safety_event") == "safety_timeout" for e in esc)


@pytest.mark.asyncio
async def test_failsafe_dedupes_p0_instead_of_crashing(db_session, test_session_row):
    """Regression for the bug found by force: a RUNAWAY response escalates a
    node-less P0, then the fail-safe escalates a SECOND node-less P0
    (safety_timeout). The index is NULLS NOT DISTINCT (one node-less pending
    P0 per session), so a raw insert would crash the fail-safe. The dedup
    fix must collapse the second into the first — no IntegrityError — while
    still force-terminating the triggering process and recording the
    timeout."""
    triggering = await _running_loop(db_session, test_session_row.id)

    outcome = await SafetyLoop().run(
        SafetyRiskType.RUNAWAY_LOOP,
        {"triggering_loop_run_id": triggering.id, "detail": "runaway"},
        test_session_row.id, db_session,
        response_hook=_slow_response,
    )

    assert outcome.fail_safe_triggered is True                  # did not crash
    assert outcome.terminated_process_id == triggering.id

    trig_after = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == triggering.id)
    )).scalar_one()
    assert trig_after.status == LoopStatus.FORCED_TERMINATION

    # Exactly ONE pending P0 for the session (the second collapsed in).
    p0s = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.session_id == test_session_row.id,
            PendingEscalation.priority_class == PriorityClass.P0,
            PendingEscalation.status == EscalationStatus.PENDING,
        )
    )).scalars().all()
    assert len(p0s) == 1

    # The timeout signal is not lost — recorded in the safety loop_run notes.
    safety_row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.safety_loop_run_id)
    )).scalar_one()
    assert safety_row.status == LoopStatus.FORCED_TERMINATION
    assert "safety_timeout" in (safety_row.notes or "")


@pytest.mark.asyncio
async def test_within_budget_response_completes_not_terminated(db_session, test_session_row):
    """A fast response completes cleanly — the fail-safe does NOT fire."""
    outcome = await SafetyLoop().run(
        SafetyRiskType.UNAPPROVED_ACTION, {"detail": "POST blocked"},
        test_session_row.id, db_session,
    )
    assert outcome.fail_safe_triggered is False
    assert outcome.risk_neutralized is True
    row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.safety_loop_run_id)
    )).scalar_one()
    assert row.status == LoopStatus.COMPLETED


# ---------------------------------------------------------------------------
# The five §16.4 risk responses (orchestration, not reinvention)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invariant_response_terminates_subagents_and_suspends_goal(db_session, test_session_row):
    from aether.models.runtime import SubAgent

    sa = SubAgent(
        name="test.safety_dummy_sa", parent_agent="legal", domain="legal", description="d",
        trigger_event="session.opened", termination_condition="returns",
        max_duration_ms=30000, max_iterations=1, authority_level=0, phase_introduced=1,
    )
    db_session.add(sa)
    await db_session.commit()
    sub_run = SubAgentRun(
        sub_agent_id=sa.id, session_id=test_session_row.id, parent_agent="legal",
        status=SubAgentStatus.SPAWNED,
    )
    goal = await _running_loop(db_session, test_session_row.id, loop_type=LoopType.GOAL)
    db_session.add(sub_run)
    await db_session.commit()

    outcome = await SafetyLoop().run(
        SafetyRiskType.INVARIANT, {"detail": "INV-03 violation", "node_id": None},
        test_session_row.id, db_session,
    )

    sa_after = (await db_session.execute(
        select(SubAgentRun).where(SubAgentRun.id == sub_run.id)
    )).scalar_one()
    goal_after = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == goal.id)
    )).scalar_one()
    assert sa_after.status == SubAgentStatus.FORCE_TERMINATED   # sub-agents terminated
    assert goal_after.status == LoopStatus.FAILED               # goal loop suspended
    # p0 safety_alert escalation
    esc = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.escalation_type == EscalationType.SAFETY_ALERT,
            PendingEscalation.priority_class == PriorityClass.P0,
        )
    )).scalars().all()
    assert len(esc) >= 1
    assert outcome.risk_neutralized is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "risk_type, expected_priority",
    [
        (SafetyRiskType.RUNAWAY_LOOP, PriorityClass.P0),
        (SafetyRiskType.AUTHORITY, PriorityClass.P1),
        (SafetyRiskType.CONTRADICTION_CLUSTER, PriorityClass.P1),
        (SafetyRiskType.UNAPPROVED_ACTION, PriorityClass.P1),
    ],
)
async def test_each_risk_escalates_at_spec_priority(db_session, test_session_row, risk_type, expected_priority):
    ctx = {"detail": "x", "pillar": "legal", "triggering_loop_run_id": None}
    outcome = await SafetyLoop().run(risk_type, ctx, test_session_row.id, db_session)
    assert outcome.escalation_required is True
    esc = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.session_id == test_session_row.id,
            PendingEscalation.escalation_type == EscalationType.SAFETY_ALERT,
            PendingEscalation.priority_class == expected_priority,
        )
    )).scalars().all()
    assert len(esc) >= 1
