"""
aether.loops.reflection_loop
==============================

The Reflection Loop (Implementation Plan §16.2). Runs after every
session close: post-session evaluation of memory quality, skill
performance, and synthesis triggers.

Exact §16.2 execution sequence (EC-21 requires this order):
  STEP 1: evaluative.memory_integrity_checker(session_id, full_scan=false)
  STEP 2: evaluative.confidence_auditor(session_id, scope="session")
  STEP 3: evaluative.skill_performance_tracker(session_id)
          -> flags below-threshold skills -> p3 escalations (in-skill)
  STEP 4: if nodes_written_since_last_synthesis >= threshold: run_synthesis()
  STEP 5: UPDATE synthesis_runs: completed_at, diff_report, reviewed_by_user
  STEP 6: pending_escalations for each violation / improvement_signal
          (steps 1-3's skills already insert their own escalations;
           this step ensures anything else surfaced is escalated)
  TERMINATE: emit reflection_complete

FAIL-SAFE (EC-21, §16.2): a Reflection Loop failure must NOT block the
next session from opening. ``run()`` therefore never raises — any error
inside the sequence is caught, the loop_run is marked FAILED, and
control returns normally. The loop is bounded (3 passes / 5 min); the
LoopWatchdog force-terminates a runaway.

Implementation note (flagged): §16.2 calls this "background, does not
block next session". It is invoked inline-but-guarded from close_session
rather than as a detached task, specifically so EC-21's "fail-safe
verified by test, not assumed" is deterministically testable. The
fail-safe semantics (never blocks the next session) are identical either
way; a production deployment can detach run() to a worker unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.config import settings
from aether.memory.synthesis import run_synthesis
from aether.models.enums import LoopStatus, LoopType
from aether.models.logs import SynthesisRun
from aether.models.nodes import Node
from aether.models.runtime import LoopRun
from aether.skills.invoker import invoke_skill

logger = logging.getLogger("aether.loops.reflection_loop")


class ReflectionLoop:
    MAX_ITERATIONS: int = 3
    MAX_DURATION_MS: int = 300000  # 5 minutes

    async def run(
        self, session_id: UUID, db: AsyncSession, *, trigger: str = "session.closed"
    ) -> LoopRun:
        loop_run = LoopRun(
            loop_type=LoopType.REFLECTION,
            trigger=trigger,
            session_id=session_id,
            max_iterations=self.MAX_ITERATIONS,
            max_duration_ms=self.MAX_DURATION_MS,
            status=LoopStatus.RUNNING,
            iteration_count=1,
        )
        db.add(loop_run)
        await db.flush()
        loop_run_id = loop_run.id

        try:
            await self._sequence(session_id, loop_run_id, db)
            loop_run.status = LoopStatus.COMPLETED
        except Exception:  # FAIL-SAFE: never propagate (EC-21)
            logger.exception("Reflection Loop failed for session %s; marking FAILED", session_id)
            loop_run.status = LoopStatus.FAILED

        loop_run.end_time = datetime.now(timezone.utc)
        await db.flush()
        logger.info("reflection_complete: session=%s status=%s", session_id, loop_run.status.value)
        return loop_run

    async def _sequence(self, session_id: UUID, loop_run_id: UUID, db: AsyncSession) -> None:
        sid = str(session_id)

        # STEP 1: memory integrity
        await invoke_skill(
            "evaluative.memory_integrity_checker",
            {"session_id": sid, "full_scan": False},
            session_id, "reflection_loop", loop_run_id, db,
        )
        # STEP 2: confidence audit (session scope)
        await invoke_skill(
            "evaluative.confidence_auditor",
            {"session_id": sid, "scope": "session", "synthesis_run_id": None},
            session_id, "reflection_loop", loop_run_id, db,
        )
        # STEP 3: skill performance (flags -> p3 escalations inside the skill)
        await invoke_skill(
            "evaluative.skill_performance_tracker",
            {"session_id": sid},
            session_id, "reflection_loop", loop_run_id, db,
        )

        # STEP 4: conditional synthesis
        last = (
            await db.execute(
                select(SynthesisRun.completed_at)
                .where(SynthesisRun.completed_at.isnot(None))
                .order_by(SynthesisRun.completed_at.desc())
                .limit(1)
            )
        ).first()
        since = last[0] if last else None
        q = select(func.count()).select_from(Node)
        if since is not None:
            q = q.where(Node.created_at > since)
        nodes_since = (await db.execute(q)).scalar_one()

        if nodes_since >= settings.synthesis_threshold_nodes:
            synth_run = await run_synthesis("reflection", db)
            # STEP 5: ensure the run carries a diff_report and review flag.
            if synth_run is not None:
                if synth_run.diff_report is None:
                    synth_run.diff_report = {
                        "new_nodes": [], "updated_nodes": [], "contradictions": [],
                        "superseded": [], "trigger": "reflection",
                    }
                synth_run.reviewed_by_user = False
                if synth_run.completed_at is None:
                    synth_run.completed_at = datetime.now(timezone.utc)
                await db.flush()

        # STEP 6: violation/improvement escalations are inserted by the
        # evaluative skills in steps 1-3 (memory_integrity_checker and
        # confidence_auditor insert p2 clarification escalations per
        # violation; skill_performance_tracker inserts p3 for flagged
        # skills). No additional escalation source remains in this
        # sequence, so step 6 is satisfied by those in-skill inserts.
