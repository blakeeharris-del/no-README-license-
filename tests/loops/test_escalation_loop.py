"""
tests.loops.test_escalation_loop — Escalation Loop (Impl Plan §16.5).

Governing exit criteria, proven empirically (not from config):
  EC-31: a real P0 signal is surfaced within 60 seconds of trigger —
         measured as actual wall-clock trigger->surfaced against real
         Postgres, and the measured value is reported.
  EC-29: the loop produces a real, bounded loop_runs row; the bounds are
         forced (watchdog terminates a row past iteration and duration
         limits), not merely configured.

Plus the seam to the Correction Loop (consuming an accurate retries count)
and the P0 uniqueness constraint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aether.loops.correction_loop import CorrectionErrorType, CorrectionLoop
from aether.loops.escalation_loop import EscalationLoop, EscalationSignal
from aether.loops.watchdog import LoopWatchdog
from aether.models.enums import (
    EscalationStatus,
    EscalationType,
    LoopStatus,
    LoopType,
    PriorityClass,
)
from aether.models.logs import ActionLog
from aether.models.runtime import LoopRun, PendingEscalation


def _p0_deadline_signal(node_id=None):
    """A signal that genuinely scores P0: an imminent legal deadline from
    the user (impact 5, time_sensitivity 5)."""
    content = {"title": "Court filing due tomorrow", "description": "Response brief deadline"}
    if node_id is not None:
        content["node_id"] = str(node_id)
    return EscalationSignal(
        escalation_type=EscalationType.P0_SIGNAL,
        claimed_priority=PriorityClass.P0,
        content=content,
        scoring_inputs={"type": "deadline", "pillar": "legal", "source": "user", "days_until": 1},
    )


# ---------------------------------------------------------------------------
# EC-31: real P0 surfaced within 60s, measured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ec31_p0_surfaced_within_60s_measured(db_session, test_session_row, capsys):
    signal = _p0_deadline_signal()

    trigger_at = datetime.now(timezone.utc)                 # trigger
    outcome = await EscalationLoop().run(signal, test_session_row.id, db_session)
    await db_session.flush()
    surfaced_at = datetime.now(timezone.utc)                # surfaced

    elapsed_s = (surfaced_at - trigger_at).total_seconds()

    # Surfaced artifacts actually exist (not just a return value):
    row = (await db_session.execute(
        select(PendingEscalation).where(PendingEscalation.id == outcome.escalation_id)
    )).scalar_one()
    assert row.priority_class == PriorityClass.P0
    assert row.status == EscalationStatus.PENDING
    surface_log = (await db_session.execute(
        select(ActionLog).where(
            ActionLog.session_id == test_session_row.id,
            ActionLog.output_summary.like("escalation surfaced:%"),
        )
    )).scalars().all()
    assert len(surface_log) == 1
    assert outcome.interrupt is True                        # P0 interrupts (STEP 5)

    # EC-31 empirical bound + reported measurement.
    with capsys.disabled():
        print(f"\n[EC-31] measured P0 trigger->surfaced latency: {elapsed_s * 1000:.1f} ms "
              f"({elapsed_s:.4f} s), bound 60 s")
    assert elapsed_s < 60.0


# ---------------------------------------------------------------------------
# Seam: consume the corrected correction_exhaust retries count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumes_accurate_correction_retries(db_session, test_session_row):
    """Run the real Correction Loop to exhaustion (retries=3), then have the
    Escalation Loop consume that queued correction_exhaust row and confirm
    it reads and acts on the accurate retries value — not that it merely
    drains the queue."""
    parent = LoopRun(
        loop_type=LoopType.GOAL, trigger="test", session_id=test_session_row.id,
        status=LoopStatus.RUNNING, iteration_count=0, max_iterations=10, max_duration_ms=120000,
    )
    db_session.add(parent)
    await db_session.commit()

    async def always_fail(attempt, error_type):
        return False

    await CorrectionLoop().run(
        error_type=CorrectionErrorType.SKILL_FAILURE,
        parent_loop_run_id=parent.id, session_id=test_session_row.id,
        db=db_session, retry_fn=always_fail,
    )
    queued = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.escalation_type == EscalationType.CORRECTION_EXHAUST,
        )
    )).scalars().one()
    assert queued.content["retries"] == 3                   # the corrected value

    outcome = await EscalationLoop().run_for_pending(queued.id, test_session_row.id, db_session)

    # Consumed in place (no duplicate row), kept P1 (mandated floor).
    assert outcome.escalation_id == queued.id
    assert outcome.priority_class == PriorityClass.P1
    all_ce = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.escalation_type == EscalationType.CORRECTION_EXHAUST,
        )
    )).scalars().all()
    assert len(all_ce) == 1                                 # enriched, not duplicated

    # It read and ACTED ON the accurate count: it's in the surfaced text.
    enriched = (await db_session.execute(
        select(PendingEscalation).where(PendingEscalation.id == queued.id)
    )).scalar_one()
    assert "3 retries" in enriched.content["escalation_text"]
    assert outcome.escalation_text and "3 retries" in outcome.escalation_text


# ---------------------------------------------------------------------------
# EC-29: real bounded loop_runs row, bounds forced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ec29_produces_real_bounded_loop_run(db_session, test_session_row):
    outcome = await EscalationLoop().run(
        _p0_deadline_signal(), test_session_row.id, db_session
    )
    row = (await db_session.execute(
        select(LoopRun).where(LoopRun.id == outcome.escalation_loop_run_id)
    )).scalar_one()
    assert row.loop_type == LoopType.ESCALATION
    assert row.max_iterations == 1 and row.max_duration_ms == 10000
    assert row.status == LoopStatus.COMPLETED
    assert row.iteration_count == 1
    assert row.end_time is not None


@pytest.mark.asyncio
async def test_ec29_iteration_bound_force_terminated_by_watchdog(db_session, test_session_row):
    """max_iterations=1 forced: a row already at its iteration limit is
    force-terminated by the watchdog (iteration_count >= max_iterations)."""
    row = LoopRun(
        loop_type=LoopType.ESCALATION, trigger="test", session_id=test_session_row.id,
        status=LoopStatus.RUNNING, iteration_count=1,
        max_iterations=EscalationLoop.MAX_ITERATIONS, max_duration_ms=EscalationLoop.MAX_DURATION_MS,
    )
    db_session.add(row)
    await db_session.commit()

    await LoopWatchdog._check_loops(db_session)

    terminated = (await db_session.execute(select(LoopRun).where(LoopRun.id == row.id))).scalar_one()
    assert terminated.status == LoopStatus.FORCED_TERMINATION


@pytest.mark.asyncio
async def test_ec29_duration_bound_force_terminated_by_watchdog(db_session, test_session_row):
    old_start = datetime.now(timezone.utc) - timedelta(
        milliseconds=EscalationLoop.MAX_DURATION_MS + 5000
    )
    row = LoopRun(
        loop_type=LoopType.ESCALATION, trigger="test", session_id=test_session_row.id,
        status=LoopStatus.RUNNING, iteration_count=0,
        max_iterations=EscalationLoop.MAX_ITERATIONS, max_duration_ms=EscalationLoop.MAX_DURATION_MS,
        start_time=old_start,
    )
    db_session.add(row)
    await db_session.commit()

    await LoopWatchdog._check_loops(db_session)

    terminated = (await db_session.execute(select(LoopRun).where(LoopRun.id == row.id))).scalar_one()
    assert terminated.status == LoopStatus.FORCED_TERMINATION


# ---------------------------------------------------------------------------
# P0 uniqueness: uq_pe_p0_pending_per_node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_p0_for_same_node_is_deduped_not_errored(db_session, test_session_row):
    """§16.5 STEP 4 writes P0 rows. A repeat P0 for the same node must be
    recognized and skipped (PB-03), not crash the loop — one pending P0
    row survives."""
    node_id = "11111111-1111-1111-1111-111111111111"

    first = await EscalationLoop().run(
        _p0_deadline_signal(node_id=node_id), test_session_row.id, db_session
    )
    assert first.deduped is False and first.interrupt is True

    second = await EscalationLoop().run(
        _p0_deadline_signal(node_id=node_id), test_session_row.id, db_session
    )
    assert second.deduped is True
    assert second.interrupt is False                        # not re-interrupted
    assert second.escalation_id == first.escalation_id      # points at the existing row

    rows = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.session_id == test_session_row.id,
            PendingEscalation.priority_class == PriorityClass.P0,
            PendingEscalation.content["node_id"].astext == node_id,
        )
    )).scalars().all()
    assert len(rows) == 1                                    # constraint's intent held


@pytest.mark.asyncio
async def test_p0_unique_index_is_the_real_backstop(db_session, test_session_row):
    """The DB partial-unique index is the concurrency backstop behind the
    loop's pre-check: two raw pending P0 rows for the same node collide."""
    node_id = "22222222-2222-2222-2222-222222222222"
    common = dict(
        escalation_type=EscalationType.P0_SIGNAL, priority_class=PriorityClass.P0,
        session_id=test_session_row.id, status=EscalationStatus.PENDING,
    )
    db_session.add(PendingEscalation(content={"title": "a", "node_id": node_id}, **common))
    await db_session.flush()
    db_session.add(PendingEscalation(content={"title": "b", "node_id": node_id}, **common))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# STEP 2 anti-inflation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_inflation_prevented_by_signal_scorer(db_session, test_session_row):
    """A signal CLAIMING P0 but scoring below it is downgraded — signal_scorer
    is a ceiling on urgency (STEP 2)."""
    signal = EscalationSignal(
        escalation_type=EscalationType.P0_SIGNAL,
        claimed_priority=PriorityClass.P0,
        content={"title": "Routine task", "description": "low-stakes item"},
        scoring_inputs={"type": "task", "pillar": "health", "source": "system", "days_until": None},
    )
    outcome = await EscalationLoop().run(signal, test_session_row.id, db_session)
    assert outcome.priority_class != PriorityClass.P0        # inflation prevented
    assert outcome.interrupt is False
