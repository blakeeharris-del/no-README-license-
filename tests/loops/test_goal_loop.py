"""tests.loops.test_goal_loop — Phase-0 Prompt Section 23."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from aether.invariants.guards import LoopLimitExceeded, WatchdogNotRunningError
from aether.loops.goal_loop import GoalLoop
from aether.loops.watchdog import LoopWatchdog
from aether.models.enums import LoopStatus, LoopType
from aether.models.runtime import LoopRun


@pytest.fixture(autouse=True)
async def _ensure_watchdog_running():
    await LoopWatchdog.start(lambda: __import__("aether.database", fromlist=["AsyncSessionLocal"]).AsyncSessionLocal())
    yield
    await LoopWatchdog.stop()


@pytest.mark.asyncio
async def test_loop_creates_loop_run_row_with_max_values_set_on_start(db_session, test_session_row):
    loop_run_id = await GoalLoop().start(test_session_row.id, db_session)
    loop_run = (await db_session.execute(select(LoopRun).where(LoopRun.id == loop_run_id))).scalar_one()
    assert loop_run.max_iterations == GoalLoop.MAX_ITERATIONS
    assert loop_run.max_duration_ms == GoalLoop.MAX_DURATION_MS
    assert loop_run.status == LoopStatus.RUNNING
    assert loop_run.iteration_count == 0


@pytest.mark.asyncio
async def test_loop_watchdog_must_be_running_before_start(db_session, test_session_row):
    await LoopWatchdog.stop()
    LoopWatchdog._running = False
    with pytest.raises(WatchdogNotRunningError):
        await GoalLoop().start(test_session_row.id, db_session)


@pytest.mark.asyncio
async def test_loop_increments_iteration_count_per_execute_turn(db_session, test_session_row, mock_llm_client):
    mock_llm_client.set_responses([
        json.dumps({"action_type": "query", "subject": "x", "implied_pillars": ["legal"], "urgency": "standard", "time_horizon": None, "entities": []}),
        json.dumps({"response": "ok", "write_proposals": [], "action_requests": [], "confidence": "explicit", "source_node_ids": [], "warnings": []}),
    ])
    loop_run_id = await GoalLoop().start(test_session_row.id, db_session)
    await GoalLoop().execute_turn(loop_run_id, "hello", test_session_row.id, db_session)
    loop_run = (await db_session.execute(select(LoopRun).where(LoopRun.id == loop_run_id))).scalar_one()
    assert loop_run.iteration_count == 1


@pytest.mark.asyncio
async def test_loop_completes_with_correct_status_on_close(db_session, test_session_row):
    loop_run_id = await GoalLoop().start(test_session_row.id, db_session)
    await GoalLoop().complete(loop_run_id, LoopStatus.COMPLETED, db_session)
    loop_run = (await db_session.execute(select(LoopRun).where(LoopRun.id == loop_run_id))).scalar_one()
    assert loop_run.status == LoopStatus.COMPLETED
    assert loop_run.end_time is not None


@pytest.mark.asyncio
async def test_loop_raises_on_iteration_limit(db_session, test_session_row):
    loop_run = LoopRun(loop_type=LoopType.GOAL, trigger="user_input", session_id=test_session_row.id,
                        status=LoopStatus.RUNNING, iteration_count=10, max_iterations=10, max_duration_ms=120000)
    db_session.add(loop_run)
    await db_session.commit()
    with pytest.raises(LoopLimitExceeded):
        await GoalLoop().execute_turn(loop_run.id, "one more", test_session_row.id, db_session)


@pytest.mark.asyncio
async def test_loop_raises_on_duration_limit(db_session, test_session_row):
    """
    execute_turn() checks elapsed time against GoalLoop.MAX_DURATION_MS
    (the fixed class constant, 120000ms) — not loop_run.max_duration_ms
    (the row's own stored value, which is what LoopWatchdog's separate,
    external check reads instead). Section 17's own pseudocode uses the
    bare class-constant names directly. start_time must therefore be
    older than the real 120-second threshold to trigger this.
    """
    from datetime import datetime, timedelta, timezone

    loop_run = LoopRun(loop_type=LoopType.GOAL, trigger="user_input", session_id=test_session_row.id,
                        status=LoopStatus.RUNNING, iteration_count=0, max_iterations=10, max_duration_ms=120000,
                        start_time=datetime.now(timezone.utc) - timedelta(seconds=121))
    db_session.add(loop_run)
    await db_session.commit()
    with pytest.raises(LoopLimitExceeded):
        await GoalLoop().execute_turn(loop_run.id, "too slow", test_session_row.id, db_session)
