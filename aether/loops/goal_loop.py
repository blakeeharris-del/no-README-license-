"""
aether.loops.goal_loop
=========================

``GoalLoop`` — the primary work loop, activated per user-directed task
(Phase-0 Prompt Section 17). Bounded by INV-08.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aether.agents.master_agent import MasterAgent
from aether.invariants.guards import WatchdogNotRunningError, assert_loop_within_bounds
from aether.loops.watchdog import LoopWatchdog
from aether.models.enums import (
    ActionType,
    AgentName,
    EscalationStatus,
    EscalationType,
    LoopStatus,
    LoopType,
    PriorityClass,
)
from aether.models.logs import ActionLog
from aether.models.runtime import LoopRun, PendingEscalation
from aether.schemas.agent import AgentResponse

logger = logging.getLogger("aether.loops.goal_loop")


class GoalLoop:
    MAX_ITERATIONS: int = 10
    MAX_DURATION_MS: int = 120000

    async def start(self, session_id: UUID, db: AsyncSession) -> UUID:
        """
        PRECONDITION: LoopWatchdog must be healthy (Risk RISK-L01) —
        raises ``WatchdogNotRunningError`` otherwise, refusing to start
        a loop nothing is watching.
        """
        if not await LoopWatchdog.is_healthy():
            raise WatchdogNotRunningError("LoopWatchdog is not running; refusing to start GoalLoop")

        loop_run = LoopRun(
            loop_type=LoopType.GOAL,
            trigger="user_input",
            session_id=session_id,
            max_iterations=self.MAX_ITERATIONS,
            max_duration_ms=self.MAX_DURATION_MS,
            status=LoopStatus.RUNNING,
            iteration_count=0,
        )
        db.add(loop_run)
        await db.commit()
        return loop_run.id

    async def execute_turn(
        self, loop_run_id: UUID, user_input: str, session_id: UUID, db: AsyncSession
    ) -> AgentResponse:
        # ---- STEP 1: bounds check [INV-08] -----------------------------------
        loop_run = (await db.execute(select(LoopRun).where(LoopRun.id == loop_run_id))).scalar_one()
        start_time = loop_run.start_time
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        elapsed_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        assert_loop_within_bounds(
            loop_run.iteration_count, self.MAX_ITERATIONS, elapsed_ms, self.MAX_DURATION_MS,
            "goal_loop", "GoalLoop.execute_turn",
        )

        # ---- STEP 2: increment iteration count ---------------------------------
        await db.execute(
            update(LoopRun).where(LoopRun.id == loop_run_id).values(iteration_count=loop_run.iteration_count + 1)
        )
        await db.commit()
        new_iteration = loop_run.iteration_count + 1

        # ---- STEP 3: delegate to Master Agent --------------------------------
        response = await MasterAgent().process(user_input, session_id, db)

        # ---- STEP 4: lightweight sanity check ------------------------------------
        # Deviation flagged for review: Section 17 says "Validate with
        # output_validator. If invalid: re-prompt once." but
        # MasterAgent.process() (Section 18, step 7) already runs the
        # real output_validator pass against the actual format_spec
        # from the context packet, and returns a plain AgentResponse —
        # not the raw LLM output dict output_validator expects, and not
        # the format_spec that validated it. Re-invoking
        # output_validator at this layer would require fabricating both
        # inputs. Implemented instead as a minimal sanity check on what
        # this layer actually has: a non-empty response.
        if not response.text:
            logger.warning("GoalLoop: MasterAgent returned an empty response text")
            response = AgentResponse(text="No response could be generated for this request.")

        # ---- STEP 5: action_log entry [INV-01] -------------------------------------
        db.add(
            ActionLog(
                session_id=session_id,
                agent=AgentName.MASTER,
                action_type=ActionType.SURFACE,
                output_summary=f"goal_loop turn {new_iteration}"[:500],
            )
        )
        await db.commit()

        # ---- STEP 6: escalate pending approvals ------------------------------------
        for approval in response.pending_approvals:
            db.add(
                PendingEscalation(
                    escalation_type=EscalationType.CLARIFICATION,
                    priority_class=PriorityClass.P1,
                    content={
                        "title": "Action awaiting approval",
                        "description": f"{approval.action} on {approval.target}",
                    },
                    session_id=session_id,
                    status=EscalationStatus.PENDING,
                )
            )
        if response.pending_approvals:
            await db.commit()

        return response

    async def complete(self, loop_run_id: UUID, status: LoopStatus, db: AsyncSession) -> None:
        await db.execute(
            update(LoopRun)
            .where(LoopRun.id == loop_run_id, LoopRun.status == LoopStatus.RUNNING)
            .values(status=status, end_time=datetime.now(timezone.utc))
        )
        await db.commit()
