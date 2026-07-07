"""
tests.loops.test_correction_loop — Correction Loop (Impl Plan §16.3).

Governing exit criteria, both proven by force (not by reading config):
  EC-30: the no-recursion rule — a correction loop cannot spawn another
         correction loop. Forced by triggering a correction whose parent
         IS a correction loop and confirming STEP 4 (retry) never runs.
  EC-29: the correction loop produces a real, bounded loop_runs row, and
         the bounds actually terminate it — forced by an always-failing
         retry (iteration bound) and a backdated row + watchdog (duration
         bound).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from aether.loops.correction_loop import CorrectionErrorType, CorrectionLoop
from aether.loops.watchdog import LoopWatchdog
from aether.models.enums import (
    ConfidenceLevel,
    CreatedByAgent,
    EscalationType,
    LoopStatus,
    LoopType,
    NodeSource,
    NodeStatus,
    PriorityClass,
)
from aether.models.nodes import Node
from aether.models.runtime import LoopRun, PendingEscalation


async def _make_loop_run(db, session_id, loop_type, *, status=LoopStatus.RUNNING,
                         iteration_count=0, start_time=None):
    row = LoopRun(
        loop_type=loop_type, trigger="test", session_id=session_id,
        status=status, iteration_count=iteration_count,
        max_iterations=CorrectionLoop.MAX_ITERATIONS,
        max_duration_ms=CorrectionLoop.MAX_DURATION_MS,
        **({"start_time": start_time} if start_time is not None else {}),
    )
    db.add(row)
    await db.commit()
    return row


# ---------------------------------------------------------------------------
# EC-29: real bounded loop_runs row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correction_produces_real_bounded_loop_run(db_session, test_session_row):
    parent = await _make_loop_run(db_session, test_session_row.id, LoopType.GOAL)

    async def always_fail(attempt, error_type):
        return False

    outcome = await CorrectionLoop().run(
        error_type=CorrectionErrorType.SKILL_FAILURE,
        parent_loop_run_id=parent.id, session_id=test_session_row.id,
        db=db_session, retry_fn=always_fail,
    )
    row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.correction_loop_run_id)
    )).scalar_one()

    assert row.loop_type == LoopType.CORRECTION
    assert row.max_iterations == 3 and row.max_duration_ms == 180000
    assert row.status == LoopStatus.FAILED  # terminated (did not recover)
    assert row.end_time is not None


@pytest.mark.asyncio
async def test_iteration_bound_forces_termination_after_three_retries(db_session, test_session_row):
    """Force it past its limit: an always-failing retry must stop at
    exactly MAX_ITERATIONS attempts and escalate — never a 4th, never a
    hang."""
    parent = await _make_loop_run(db_session, test_session_row.id, LoopType.GOAL)

    calls = []

    async def always_fail(attempt, error_type):
        calls.append(attempt)
        return False

    outcome = await CorrectionLoop().run(
        error_type=CorrectionErrorType.OUTPUT_INVALID,
        parent_loop_run_id=parent.id, session_id=test_session_row.id,
        db=db_session, retry_fn=always_fail,
    )

    assert calls == [1, 2, 3]              # exactly 3 attempts, then stop
    assert outcome.recovered is False
    assert outcome.retries == 3
    assert outcome.escalated is True and outcome.reason == "retries_exhausted"

    row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.correction_loop_run_id)
    )).scalar_one()
    assert row.iteration_count == 3
    assert row.status == LoopStatus.FAILED

    # parent goal loop marked failed (STEP 6)
    parent_row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == parent.id)
    )).scalar_one()
    assert parent_row.status == LoopStatus.FAILED

    # correction_exhaust p1 escalation queued (STEP 6)
    esc = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.session_id == test_session_row.id,
            PendingEscalation.escalation_type == EscalationType.CORRECTION_EXHAUST,
        )
    )).scalars().all()
    assert len(esc) == 1 and esc[0].priority_class == PriorityClass.P1


@pytest.mark.asyncio
async def test_duration_bound_force_terminated_by_watchdog(db_session, test_session_row):
    """The max_duration_ms bound is real for a CORRECTION row: a backdated
    running correction loop is force-terminated by the watchdog (INV-08's
    external enforcement), proving the duration limit, not just its
    configuration."""
    old_start = datetime.now(timezone.utc) - timedelta(milliseconds=CorrectionLoop.MAX_DURATION_MS + 5000)
    row = await _make_loop_run(
        db_session, test_session_row.id, LoopType.CORRECTION, start_time=old_start
    )

    await LoopWatchdog._check_loops(db_session)

    terminated = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == row.id)
    )).scalar_one()
    assert terminated.status == LoopStatus.FORCED_TERMINATION
    assert terminated.end_time is not None


# ---------------------------------------------------------------------------
# EC-30: no-recursion rule, forced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duration_breach_midretry_reports_accurate_retry_count(db_session, test_session_row):
    """Regression for the mid-retry INV-08 bug: when the duration bound
    trips *after* some retries, the correction_exhaust escalation must
    report the true number of attempts, not 0.

    Forced by backdating the loop_run's start_time on the 2nd retry so the
    3rd bound-check trips. Before the fix, the bound raised out of
    _retry_loop, run() never captured ``retries``, and the escalation said
    retries=0.
    """
    parent = await _make_loop_run(db_session, test_session_row.id, LoopType.GOAL)

    attempts = []

    async def fail_then_backdate(attempt, error_type):
        attempts.append(attempt)
        if attempt == 2:
            # Age the loop_run past its duration bound so the next
            # assert_loop_within_bounds trips.
            row = (await db_session.execute(
                select(LoopRun).where(
                    LoopRun.loop_type == LoopType.CORRECTION,
                    LoopRun.session_id == test_session_row.id,
                )
            )).scalars().first()
            row.start_time = datetime.now(timezone.utc) - timedelta(
                milliseconds=CorrectionLoop.MAX_DURATION_MS + 5000
            )
            await db_session.flush()
        return False

    outcome = await CorrectionLoop().run(
        error_type=CorrectionErrorType.TIMEOUT,
        parent_loop_run_id=parent.id, session_id=test_session_row.id,
        db=db_session, retry_fn=fail_then_backdate,
    )

    assert attempts == [1, 2]        # 3rd blocked by the duration bound
    assert outcome.retries == 2      # accurate, not 0
    assert outcome.escalated is True

    esc = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.escalation_type == EscalationType.CORRECTION_EXHAUST,
        )
    )).scalars().one()
    assert esc.content["retries"] == 2  # the fix: not understated to 0


@pytest.mark.asyncio
async def test_correction_cannot_spawn_child_correction(db_session, test_session_row):
    """EC-30 forced: a correction triggered with a CORRECTION parent must
    hit the parent_loop_type==correction guard (STEP 2), skip STEP 4
    entirely (no retry_fn call), and drop to rollback+escalation."""
    # Parent is itself a correction loop — the recursion case.
    parent_correction = await _make_loop_run(
        db_session, test_session_row.id, LoopType.CORRECTION
    )

    retry_calls = []

    async def spy_retry(attempt, error_type):
        retry_calls.append(attempt)
        return True  # would "succeed" if it were ever allowed to run

    outcome = await CorrectionLoop().run(
        error_type=CorrectionErrorType.SKILL_FAILURE,
        parent_loop_run_id=parent_correction.id, session_id=test_session_row.id,
        db=db_session, retry_fn=spy_retry,
    )

    # The guard blocked recursion: the retry cycle never ran.
    assert retry_calls == []
    assert outcome.recovered is False
    assert outcome.retries == 0
    assert outcome.escalated is True
    assert outcome.reason == "recursion_blocked"

    # The child correction row exists but terminated without recursing.
    child = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.correction_loop_run_id)
    )).scalar_one()
    assert child.loop_type == LoopType.CORRECTION
    assert child.iteration_count == 0
    assert child.status == LoopStatus.FAILED
    assert child.notes == "correction_exhaust: recursion_blocked"

    # No grandchild: exactly two CORRECTION rows exist (the parent + this
    # one), never a third.
    correction_rows = (await db_session.execute(
        select(LoopRun).where(
            LoopRun.session_id == test_session_row.id,
            LoopRun.loop_type == LoopType.CORRECTION,
        )
    )).scalars().all()
    assert len(correction_rows) == 2


# ---------------------------------------------------------------------------
# STEP 3 (invariant) and STEP 4 (recovery) paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invariant_error_skips_retry(db_session, test_session_row):
    """STEP 3: an INVARIANT error cannot be retried — skip to escalation."""
    parent = await _make_loop_run(db_session, test_session_row.id, LoopType.GOAL)

    retry_calls = []

    async def spy_retry(attempt, error_type):
        retry_calls.append(attempt)
        return True

    outcome = await CorrectionLoop().run(
        error_type=CorrectionErrorType.INVARIANT,
        parent_loop_run_id=parent.id, session_id=test_session_row.id,
        db=db_session, retry_fn=spy_retry,
    )
    assert retry_calls == []
    assert outcome.reason == "invariant"
    assert outcome.escalated is True


@pytest.mark.asyncio
async def test_successful_retry_recovers_without_escalating(db_session, test_session_row):
    """STEP 4 SUCCESS: a retry that succeeds resumes the parent and the
    correction completes — no escalation, parent left RUNNING."""
    parent = await _make_loop_run(db_session, test_session_row.id, LoopType.GOAL)

    async def succeed_on_second(attempt, error_type):
        return attempt == 2

    outcome = await CorrectionLoop().run(
        error_type=CorrectionErrorType.TIMEOUT,
        parent_loop_run_id=parent.id, session_id=test_session_row.id,
        db=db_session, retry_fn=succeed_on_second,
    )
    assert outcome.recovered is True
    assert outcome.retries == 2
    assert outcome.escalated is False

    row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.correction_loop_run_id)
    )).scalar_one()
    assert row.status == LoopStatus.COMPLETED

    parent_row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == parent.id)
    )).scalar_one()
    assert parent_row.status == LoopStatus.RUNNING  # not failed — it resumes

    no_esc = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.session_id == test_session_row.id,
            PendingEscalation.escalation_type == EscalationType.CORRECTION_EXHAUST,
        )
    )).scalars().all()
    assert no_esc == []


@pytest.mark.asyncio
async def test_rollback_archives_partial_write_on_exhaustion(db_session, test_session_row):
    """STEP 5: when a node_id is supplied (a partial write), exhaustion
    archives it via rollback_executor (never deletes — INV-02)."""
    node = Node(
        type="fact", title="partial write", content="x", source=NodeSource.AGENT_WRITE,
        confidence=ConfidenceLevel.SPECULATIVE, status=NodeStatus.ACTIVE,
        created_by=CreatedByAgent.MASTER_AGENT, session_id=test_session_row.id, metadata_={},
    )
    db_session.add(node)
    await db_session.commit()

    async def always_fail(attempt, error_type):
        return False

    parent = await _make_loop_run(db_session, test_session_row.id, LoopType.GOAL)
    await CorrectionLoop().run(
        error_type=CorrectionErrorType.SKILL_FAILURE,
        parent_loop_run_id=parent.id, session_id=test_session_row.id,
        db=db_session, retry_fn=always_fail, node_id=node.id,
    )

    rolled = (await db_session.execute(select(Node).where(Node.id == node.id))).scalar_one()
    assert rolled.status == NodeStatus.ARCHIVED  # archived, not deleted
