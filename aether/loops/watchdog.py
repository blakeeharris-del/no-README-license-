"""
aether.loops.watchdog
========================

``LoopWatchdog`` — async background task enforcing INV-08 (Phase-0
Prompt Section 16). Must be running before any ``loop_runs`` row is
created; ``GoalLoop.start()`` (Step 20) checks ``is_healthy()`` first
and refuses to start otherwise (Risk RISK-L01).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.enums import (
    ActionType,
    AgentName,
    EscalationStatus,
    EscalationType,
    LoopStatus,
    PriorityClass,
)
from aether.models.logs import ActionLog
from aether.models.runtime import LoopRun, PendingEscalation

logger = logging.getLogger("aether.loops.watchdog")


class LoopWatchdog:
    """
    Async background task. Polls ``loop_runs`` every
    ``CHECK_INTERVAL_MS``. Enforces INV-08. Cleans stale
    ``skill_invocation_log`` rows.

    Class-level state (not instance state) — there is exactly one
    watchdog per process, matching the spec's ``@classmethod``-only
    design.
    """

    CHECK_INTERVAL_MS: int = 1000
    STALE_SKILL_LOG_THRESHOLD_MS: int = 60000

    _task: asyncio.Task | None = None
    _running: bool = False

    @classmethod
    async def start(cls, db_factory) -> None:
        """Start the watchdog background task."""
        cls._running = True
        cls._task = asyncio.create_task(cls._watch(db_factory))

    @classmethod
    async def _watch(cls, db_factory) -> None:
        while cls._running:
            await asyncio.sleep(cls.CHECK_INTERVAL_MS / 1000)
            try:
                async with db_factory() as db:
                    await cls._check_loops(db)
                    await cls._check_stale_skill_logs(db)
            except Exception as e:
                logger.error("Watchdog cycle error", extra={"error": str(e)})

    @classmethod
    async def _check_loops(cls, db: AsyncSession) -> None:
        """
        Query ``loop_runs`` WHERE status='running'. For each loop,
        force-terminate it if it has exceeded its iteration count or
        duration, then escalate (P0) and log the action.
        """
        running_loops = (
            await db.execute(select(LoopRun).where(LoopRun.status == LoopStatus.RUNNING))
        ).scalars().all()

        now = datetime.now(timezone.utc)
        for loop in running_loops:
            start_time = loop.start_time
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            elapsed_ms = (now - start_time).total_seconds() * 1000
            overdue = loop.iteration_count >= loop.max_iterations or elapsed_ms > loop.max_duration_ms

            if not overdue:
                continue

            await db.execute(
                update(LoopRun)
                .where(LoopRun.id == loop.id)
                .values(status=LoopStatus.FORCED_TERMINATION, end_time=now)
            )
            db.add(
                PendingEscalation(
                    escalation_type=EscalationType.SAFETY_ALERT,
                    priority_class=PriorityClass.P0,
                    content={
                        "title": "Loop terminated",
                        "loop_run_id": str(loop.id),
                        "loop_type": loop.loop_type.value,
                        "reason": "max_iterations_or_duration_exceeded",
                        "iteration_count": loop.iteration_count,
                    },
                    session_id=loop.session_id,
                    status=EscalationStatus.PENDING,
                )
            )
            db.add(
                ActionLog(
                    session_id=loop.session_id,
                    agent=AgentName.MASTER,
                    action_type=ActionType.SURFACE,
                    output_summary=f"watchdog: forced_termination loop {loop.id}"[:500],
                )
            )
            await db.commit()
            logger.critical(
                "Watchdog forced loop termination", extra={"loop_run_id": str(loop.id)}
            )

    @classmethod
    async def _check_stale_skill_logs(cls, db: AsyncSession) -> None:
        """
        UPDATE skill_invocation_log SET status='timeout' WHERE
        status='running' AND timestamp older than
        ``STALE_SKILL_LOG_THRESHOLD_MS``. Covers the crash-recovery
        case where a process died mid-skill-invocation and never got to
        write its own COMPLETE/ERROR/TIMEOUT status.

        Real bug, found only by the EC-14 100-session stress test: the
        ``||`` string-concatenation operator in the interval expression
        below requires a text operand, but the threshold was bound as
        a Python float. asyncpg's driver strictly type-checks bind
        parameters and rejected it on every single call — silently,
        since LoopWatchdog._watch() catches and logs all per-cycle
        exceptions rather than crashing. This method had never actually
        run successfully. Fixed by binding the threshold as a string.
        """
        threshold_seconds = str(cls.STALE_SKILL_LOG_THRESHOLD_MS / 1000)
        await db.execute(
            text(
                """
                UPDATE skill_invocation_log
                SET status = 'timeout'
                WHERE status = 'running'
                  AND timestamp < now() - (:threshold_seconds || ' seconds')::interval
                """
            ),
            {"threshold_seconds": threshold_seconds},
        )
        await db.commit()

    @classmethod
    async def is_healthy(cls) -> bool:
        return cls._running and cls._task is not None and not cls._task.done()

    @classmethod
    async def stop(cls) -> None:
        cls._running = False
        if cls._task:
            cls._task.cancel()
