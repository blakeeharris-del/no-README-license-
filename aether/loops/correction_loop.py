"""
aether.loops.correction_loop
==============================

The Correction Loop (Implementation Plan §16.3). Recovers from errors
with up to 3 retries before escalating. **Cannot spawn another
correction loop** (max_correction_depth = 1) — this no-recursion rule is
the property EC-30 forces.

Exact §16.3 execution sequence (implemented below step-for-step):
  STEP 1: Classify error: SKILL_FAILURE | TIMEOUT | OUTPUT_INVALID | INVARIANT
  STEP 2: if parent_loop_type == "correction": SKIP TO STEP 5 (no recursion)
  STEP 3: if error_type == INVARIANT: SKIP TO STEP 5 (cannot retry)
  STEP 4: Attempt corrective retry (max 3):
            SKILL_FAILURE  -> retry invoke_skill with same inputs
            TIMEOUT        -> retry with reduced context
            OUTPUT_INVALID -> re-invoke with schema reminder in inputs
          if SUCCESS: resume parent goal loop -> TERMINATE
          if FAIL: increment retry_count -> loop back to STEP 4
  STEP 5: Rollback if partial write occurred: safety.rollback_executor(node_id)
  STEP 6: INSERT pending_escalations(type=correction_exhaust, p_class=p1)
          Preserve session_state snapshot
          UPDATE parent loop_runs.status=failed
          trigger escalation_loop

Bounds (§16.3): max_iterations = 3, max_duration_ms = 180,000. Enforced
by ``assert_loop_within_bounds`` (INV-08) at the top of every retry, and
externally by the LoopWatchdog on the ``loop_runs`` row — a runaway
correction is force-terminated exactly like any other loop.

STEP 2 detail (EC-30): ``loop_runs`` has ``parent_loop_run_id`` (an FK)
but no ``parent_loop_type`` column, so the parent's type is *derived* by
looking up the parent row's ``loop_type`` — the check §16.3 spells out.
When the parent is itself a CORRECTION loop, the retry cycle (STEP 4) is
skipped entirely and the loop drops straight to rollback + escalation:
no child correction ever retries, and no grandchild can be triggered.

STEP 6 deviation (documented, not silent): "trigger escalation_loop" is
the Escalation Loop's entry point, which is the *next* build entry and
does not exist yet. The durable, testable artifact STEP 6 specifies —
the ``pending_escalations(correction_exhaust, p1)`` row — IS inserted
here; the Escalation Loop will consume that queue when it is built. The
orchestration handoff is logged, not faked.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aether.invariants.guards import LoopLimitExceeded, assert_loop_within_bounds
from aether.memory.session_state import rebuild_l1, save_l1_snapshot
from aether.models.enums import (
    EscalationStatus,
    EscalationType,
    LoopStatus,
    LoopType,
    PriorityClass,
)
from aether.models.runtime import LoopRun, PendingEscalation
from aether.skills.safety.rollback_executor import rollback_executor

logger = logging.getLogger("aether.loops.correction_loop")


class CorrectionErrorType(str, enum.Enum):
    """STEP 1 error classification (runtime only — not persisted)."""

    SKILL_FAILURE = "skill_failure"
    TIMEOUT = "timeout"
    OUTPUT_INVALID = "output_invalid"
    INVARIANT = "invariant"


# A corrective retry: returns True on success. ``attempt`` is 1-based;
# ``error_type`` lets the caller vary the retry per §16.3 STEP 4
# (same inputs / reduced context / schema reminder). When no retry_fn is
# supplied there is no corrective action available, so the loop cannot
# claim success — it exhausts and escalates (safe default, never a
# silent pass).
RetryFn = Callable[..., Awaitable[bool]]


@dataclass
class CorrectionOutcome:
    correction_loop_run_id: UUID
    status: str  # "recovered" | "escalated"
    recovered: bool
    retries: int
    escalated: bool
    reason: Optional[str]  # why it escalated, if it did


class CorrectionLoop:
    MAX_ITERATIONS: int = 3
    MAX_DURATION_MS: int = 180000  # 3 minutes
    MAX_CORRECTION_DEPTH: int = 1  # a correction loop cannot spawn a correction loop

    async def run(
        self,
        *,
        error_type: CorrectionErrorType,
        parent_loop_run_id: Optional[UUID],
        session_id: UUID,
        db: AsyncSession,
        retry_fn: Optional[RetryFn] = None,
        node_id: Optional[UUID] = None,
    ) -> CorrectionOutcome:
        # Create the correction loop_runs row up front (EC-29): a triggered
        # correction always produces a real, bounded loop_runs row, even in
        # the recursion-blocked / invariant paths that skip retrying.
        loop_run = LoopRun(
            loop_type=LoopType.CORRECTION,
            trigger=f"correction:{error_type.value}",
            session_id=session_id,
            parent_loop_run_id=parent_loop_run_id,
            max_iterations=self.MAX_ITERATIONS,
            max_duration_ms=self.MAX_DURATION_MS,
            status=LoopStatus.RUNNING,
            iteration_count=0,
        )
        db.add(loop_run)
        await db.flush()

        # STEP 1: the error is classified at the trigger site (the §16.3
        # trigger conditions each map to one CorrectionErrorType) and passed
        # in as ``error_type``; nothing to re-derive here.

        # STEP 2: no-recursion rule (EC-30). Derive the parent's loop type
        # from its row — there is no parent_loop_type column.
        parent_type = await self._parent_loop_type(parent_loop_run_id, db)
        recursion_blocked = parent_type == LoopType.CORRECTION

        # STEP 3: an invariant violation cannot be retried.
        is_invariant = error_type == CorrectionErrorType.INVARIANT

        recovered = False
        retries = 0
        if not recursion_blocked and not is_invariant:
            # STEP 4: bounded corrective retry. _retry_loop absorbs an
            # INV-08 bound breach internally (returning the accurate retry
            # count) and never propagates, so escalation below always
            # reports the true number of attempts.
            recovered, retries = await self._retry_loop(loop_run, retry_fn, error_type, db)

        now = datetime.now(timezone.utc)
        if recovered:
            # STEP 4 SUCCESS: parent goal loop resumes; correction is done.
            loop_run.status = LoopStatus.COMPLETED
            loop_run.end_time = now
            loop_run.notes = f"recovered after {retries} retr{'y' if retries == 1 else 'ies'}"
            await db.flush()
            logger.info("correction_loop: recovered after %d retries", retries)
            return CorrectionOutcome(
                correction_loop_run_id=loop_run.id, status="recovered",
                recovered=True, retries=retries, escalated=False, reason=None,
            )

        # STEP 5: rollback if a partial write occurred. rollback_executor is
        # a safety skill — called directly, never via invoke_skill, and it
        # archives (never deletes, INV-02).
        if node_id is not None:
            await rollback_executor(
                node_id, db, session_id=session_id,
                reason=f"correction failed ({error_type.value})",
            )

        # STEP 6: escalate, preserve state, fail the parent, hand to escalation.
        reason = (
            "recursion_blocked" if recursion_blocked
            else "invariant" if is_invariant
            else "retries_exhausted"
        )
        db.add(
            PendingEscalation(
                escalation_type=EscalationType.CORRECTION_EXHAUST,
                priority_class=PriorityClass.P1,
                content={
                    "title": "Correction exhausted",
                    "description": f"Correction for {error_type.value} did not recover ({reason}).",
                    "error_type": error_type.value,
                    "reason": reason,
                    "retries": retries,
                },
                session_id=session_id,
                status=EscalationStatus.PENDING,
            )
        )

        # Preserve session_state snapshot (so the parent's work isn't lost).
        l1 = await rebuild_l1(session_id, db)
        await save_l1_snapshot(session_id, l1, db)

        # UPDATE parent loop_runs.status=failed (only the parent; guarded to
        # the RUNNING state so we never resurrect an already-terminal row).
        if parent_loop_run_id is not None:
            await db.execute(
                update(LoopRun)
                .where(LoopRun.id == parent_loop_run_id, LoopRun.status == LoopStatus.RUNNING)
                .values(status=LoopStatus.FAILED, end_time=now)
            )

        # The correction loop itself terminates FAILED — it ran its full
        # sequence but did not recover. Terminal status satisfies EC-29.
        loop_run.status = LoopStatus.FAILED
        loop_run.end_time = now
        loop_run.notes = f"correction_exhaust: {reason}"
        await db.flush()

        # trigger escalation_loop — DEFERRED to the Escalation Loop build
        # (next entry). The correction_exhaust escalation is already queued
        # above; this is the handoff point, logged rather than faked.
        logger.info(
            "correction_loop: escalated (%s); correction_exhaust queued for the Escalation Loop",
            reason,
        )
        return CorrectionOutcome(
            correction_loop_run_id=loop_run.id, status="escalated",
            recovered=False, retries=retries, escalated=True, reason=reason,
        )

    async def _parent_loop_type(
        self, parent_loop_run_id: Optional[UUID], db: AsyncSession
    ) -> Optional[LoopType]:
        if parent_loop_run_id is None:
            return None
        return (
            await db.execute(select(LoopRun.loop_type).where(LoopRun.id == parent_loop_run_id))
        ).scalar_one_or_none()

    async def _retry_loop(
        self,
        loop_run: LoopRun,
        retry_fn: Optional[RetryFn],
        error_type: CorrectionErrorType,
        db: AsyncSession,
    ) -> tuple[bool, int]:
        """STEP 4: up to MAX_ITERATIONS corrective retries, INV-08 bounded."""
        retries = 0
        while retries < self.MAX_ITERATIONS:
            # Read start_time fresh each iteration (not snapshotted once):
            # the elapsed-duration bound must reflect the row's current
            # start_time, including any external update, so the INV-08
            # duration check is live rather than frozen at loop entry.
            start_time = loop_run.start_time
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            elapsed_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
            # INV-08 bound check. Caught HERE, not in run(): a bug found by
            # reasoning about the mid-retry duration breach — if this raised
            # out of _retry_loop, run()'s ``recovered, retries = ...``
            # assignment would be skipped, leaving retries=0, and the
            # correction_exhaust escalation would understate the attempts
            # actually made. Absorbing the breach here returns the accurate
            # count and still terminates gracefully into rollback +
            # escalation (the watchdog would also force-terminate the row).
            # In the normal path retries<MAX_ITERATIONS so this never trips
            # and the while-condition bounds the loop.
            try:
                assert_loop_within_bounds(
                    retries, self.MAX_ITERATIONS, elapsed_ms, self.MAX_DURATION_MS,
                    "correction_loop", "CorrectionLoop._retry_loop",
                )
            except LoopLimitExceeded:
                logger.warning(
                    "correction_loop hit INV-08 bound after %d retries; escalating", retries
                )
                break

            retries += 1
            loop_run.iteration_count = retries
            await db.flush()

            success = False
            if retry_fn is not None:
                success = bool(await retry_fn(attempt=retries, error_type=error_type))
            if success:
                return True, retries

        return False, retries
