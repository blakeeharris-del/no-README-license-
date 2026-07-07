"""
tests.loops.test_shutdown_loop — Shutdown Loop (Impl Plan §16.6).

Governing criterion EC-32, proven by force against real Postgres: with a
realistic active tree (a parent goal loop, a nested Correction loop
spawned within it, and a sub_agent_run), triggering Shutdown drives
EVERY active loop_runs and sub_agent_runs row for the session to a
terminal state — asserted on actual row states, and specifically on the
nested sub-loop (the "reachable-for-termination != watched-by-parent"
gap), not just the top-level parent. Plus: reverse spawn order is real,
zero orphans remain, and the Shutdown Loop's own row is bounded and
self-excluded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from aether.loops.shutdown_loop import ShutdownLoop
from aether.loops.watchdog import LoopWatchdog
from aether.models.enums import (
    LoopStatus,
    LoopType,
    SessionStatus,
    SubAgentStatus,
)
from aether.models.runtime import LoopRun, SubAgentRun
from aether.models.sessions import Session


async def _stub_summary(_l1):
    return "test summary"


async def _make_sub_agent(db, name):
    from aether.models.runtime import SubAgent

    sa = SubAgent(
        name=name, parent_agent="legal", domain="legal",
        description="d", trigger_event="session.opened",
        termination_condition="returns", max_duration_ms=30000,
        max_iterations=1, authority_level=0, phase_introduced=1,
    )
    db.add(sa)
    await db.commit()
    return sa


async def _build_active_tree(db, session_id):
    """A realistic active tree with distinct spawn times (parent oldest,
    children newer): goal(-30s) <- correction(-20s), plus a sub_agent(-10s)."""
    now = datetime.now(timezone.utc)
    goal = LoopRun(
        loop_type=LoopType.GOAL, trigger="user_input", session_id=session_id,
        status=LoopStatus.RUNNING, iteration_count=1, max_iterations=10, max_duration_ms=120000,
        start_time=now - timedelta(seconds=30),
    )
    db.add(goal)
    await db.flush()
    correction = LoopRun(
        loop_type=LoopType.CORRECTION, trigger="correction:skill_failure", session_id=session_id,
        parent_loop_run_id=goal.id, status=LoopStatus.RUNNING, iteration_count=1,
        max_iterations=3, max_duration_ms=180000, start_time=now - timedelta(seconds=20),
    )
    db.add(correction)
    await db.flush()

    sa = await _make_sub_agent(db, name="test.shutdown_dummy_sub_agent")
    sub_run = SubAgentRun(
        sub_agent_id=sa.id, session_id=session_id, parent_agent="legal",
        loop_run_id=goal.id, status=SubAgentStatus.SPAWNED,
        spawned_at=now - timedelta(seconds=10),
    )
    db.add(sub_run)
    await db.commit()
    return goal, correction, sub_run


# ---------------------------------------------------------------------------
# EC-32: every active process terminated, including the nested sub-loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ec32_terminates_entire_active_tree(db_session, test_session_row):
    goal, correction, sub_run = await _build_active_tree(db_session, test_session_row.id)

    await ShutdownLoop().run(
        test_session_row.id, db_session, summary_fn=_stub_summary, trigger_reflection=False
    )

    # Re-query actual Postgres row states (not the return value).
    goal_after = (await db_session.execute(select(LoopRun).where(LoopRun.id == goal.id))).scalar_one()
    corr_after = (await db_session.execute(select(LoopRun).where(LoopRun.id == correction.id))).scalar_one()
    sa_after = (await db_session.execute(select(SubAgentRun).where(SubAgentRun.id == sub_run.id))).scalar_one()

    assert goal_after.status == LoopStatus.FORCED_TERMINATION
    assert goal_after.end_time is not None
    # The nested sub-loop — the row an ordinary close would miss:
    assert corr_after.status == LoopStatus.FORCED_TERMINATION
    assert corr_after.end_time is not None
    # The sub_agent_run:
    assert sa_after.status == SubAgentStatus.FORCE_TERMINATED
    assert sa_after.terminated_at is not None


@pytest.mark.asyncio
async def test_reverse_spawn_order_children_before_parents(db_session, test_session_row):
    goal, correction, sub_run = await _build_active_tree(db_session, test_session_row.id)

    outcome = await ShutdownLoop().run(
        test_session_row.id, db_session, summary_fn=_stub_summary, trigger_reflection=False
    )

    order = [t.id for t in outcome.terminated]
    # newest-first: sub_agent(-10) -> correction(-20) -> goal(-30)
    assert order == [sub_run.id, correction.id, goal.id]
    # explicit children-before-parent guarantees:
    assert order.index(correction.id) < order.index(goal.id)     # nested loop before its parent
    assert order.index(sub_run.id) < order.index(goal.id)        # sub_agent before its loop


# ---------------------------------------------------------------------------
# Zero orphans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_orphans_after_shutdown(db_session, test_session_row):
    await _build_active_tree(db_session, test_session_row.id)

    await ShutdownLoop().run(
        test_session_row.id, db_session, summary_fn=_stub_summary, trigger_reflection=False
    )

    running_loops = (await db_session.execute(
        select(LoopRun).where(
            LoopRun.session_id == test_session_row.id,
            LoopRun.status == LoopStatus.RUNNING,
        )
    )).scalars().all()
    active_subs = (await db_session.execute(
        select(SubAgentRun).where(
            SubAgentRun.session_id == test_session_row.id,
            SubAgentRun.status.in_((SubAgentStatus.SPAWNED, SubAgentStatus.RUNNING)),
        )
    )).scalars().all()

    assert running_loops == []      # zero straggler loops
    assert active_subs == []        # zero straggler sub_agents


# ---------------------------------------------------------------------------
# Self-exclusion + EC-29 bounded shutdown row + STEP 4 close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_row_bounded_completed_and_self_excluded(db_session, test_session_row):
    goal, correction, sub_run = await _build_active_tree(db_session, test_session_row.id)

    outcome = await ShutdownLoop().run(
        test_session_row.id, db_session, summary_fn=_stub_summary, trigger_reflection=False
    )

    shutdown_row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.shutdown_loop_run_id)
    )).scalar_one()
    # bounded like the others (EC-29 consistency for the SHUTDOWN loop_type)
    assert shutdown_row.loop_type == LoopType.SHUTDOWN
    assert shutdown_row.max_iterations == 1 and shutdown_row.max_duration_ms == 15000
    # single-pass terminal action: completes, not force-terminated itself
    assert shutdown_row.status == LoopStatus.COMPLETED
    # self-exclusion: the shutdown row is never in its own termination set
    assert outcome.shutdown_loop_run_id not in [t.id for t in outcome.terminated]
    assert len(outcome.terminated) == 3


@pytest.mark.asyncio
async def test_step4_closes_session_with_summary(db_session, test_session_row):
    await _build_active_tree(db_session, test_session_row.id)

    outcome = await ShutdownLoop().run(
        test_session_row.id, db_session, summary_fn=_stub_summary, trigger_reflection=False
    )

    session_after = (await db_session.execute(
        select(Session).where(Session.id == test_session_row.id)
    )).scalar_one()
    assert session_after.status == SessionStatus.CLOSED
    assert session_after.ended_at is not None
    assert session_after.summary == "test summary"
    assert outcome.session_closed is True


@pytest.mark.asyncio
async def test_shutdown_row_duration_bound_force_terminated_by_watchdog(db_session, test_session_row):
    """EC-29 consistency: a hung SHUTDOWN row is force-terminated by the
    watchdog like any other loop."""
    old_start = datetime.now(timezone.utc) - timedelta(
        milliseconds=ShutdownLoop.MAX_DURATION_MS + 5000
    )
    row = LoopRun(
        loop_type=LoopType.SHUTDOWN, trigger="shutdown_requested", session_id=test_session_row.id,
        status=LoopStatus.RUNNING, iteration_count=1,
        max_iterations=ShutdownLoop.MAX_ITERATIONS, max_duration_ms=ShutdownLoop.MAX_DURATION_MS,
        start_time=old_start,
    )
    db_session.add(row)
    await db_session.commit()

    await LoopWatchdog._check_loops(db_session)

    terminated = (await db_session.execute(select(LoopRun).where(LoopRun.id == row.id))).scalar_one()
    assert terminated.status == LoopStatus.FORCED_TERMINATION


@pytest.mark.asyncio
async def test_shutdown_with_no_active_processes_is_clean(db_session, test_session_row):
    """A session with nothing active still closes cleanly (no rows to
    terminate, no error)."""
    outcome = await ShutdownLoop().run(
        test_session_row.id, db_session, summary_fn=_stub_summary, trigger_reflection=False
    )
    assert outcome.terminated == []
    assert outcome.session_closed is True
