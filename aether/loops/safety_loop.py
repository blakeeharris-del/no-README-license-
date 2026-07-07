"""
aether.loops.safety_loop
==========================

The Safety Loop (Implementation Plan §16.4 / Missing Specs Vol 3
LOOP-04). Foundation §10.7.2 describes it as "the always-active
monitoring loop … highest priority — can interrupt all other loops."
It is NOT a discrete iterating loop like Goal/Correction: it has no
iteration limit by design (LOOP-04: ``max_iterations: null  # safety
loop has no iteration limit``). It is a per-trigger *dispatcher* — each
time one of the five §16.4 risks fires, it records a
``loop_runs(loop_type='safety', trigger=risk_type)`` row (LOOP-04
logging line 2835), executes the spec'd risk response, and enforces a
10-second per-response budget with a fail-safe force-termination.

Five risk triggers (§16.4 table / Foundation §10.7.2 line 512 / LOOP-04
``triggers``):
  runaway_loop | invariant | authority | contradiction_cluster | unapproved_action

The responses already exist piecemeal across the safety machinery
(LoopWatchdog terminates runaways; authority_checker blocks authority
violations at the gateway; action_gateway blocks unapproved externals
via INV-05; contradiction_enforcer flags clusters). This loop
ORCHESTRATES and RECORDS those responses per §16.4 — it does not
reinvent the underlying enforcement.

------------------------------------------------------------------------
CONFIG-vs-SCHEMA RECONCILIATION (documented deviation, flagged for the
Phase-2 HANDOFF — not resolved silently):

LOOP-04 line 2768 declares ``max_iterations: null`` (the safety loop has
no iteration limit). But ``loop_runs`` carries
``CHECK (max_iterations IS NOT NULL AND max_duration_ms IS NOT NULL)``
(models/runtime.py, ck_loop_runs_bounds_required) — a NULL
max_iterations cannot be stored. Reconciliation: the safety
``loop_runs`` row stores the placeholder ``max_iterations = 1`` (each
trigger is exactly one response, so "1" is the truthful single-response
count), while the REAL, forceable bound is ``max_duration_ms = 10000``
(the 10s per-response limit). EC-29 for Safety is therefore proven by
forcing the DURATION bound, never an iteration bound (there is none).
------------------------------------------------------------------------

ESCALATION-TYPE RECONCILIATION: LOOP-04 names a ``type='safety_timeout'``
escalation for the fail-safe, but ``EscalationType`` has no such member
(only p0_signal/correction_exhaust/safety_alert/clarification). Mapped to
``SAFETY_ALERT`` with ``content['safety_event'] = 'safety_timeout'`` so
the signal is still queryable. Also flagged for the HANDOFF.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aether.memory.session_state import rebuild_l1, save_l1_snapshot
from aether.models.enums import (
    ActionType,
    AgentName,
    EscalationStatus,
    EscalationType,
    LoopStatus,
    LoopType,
    PriorityClass,
    SubAgentStatus,
)
from aether.models.logs import ActionLog
from aether.models.runtime import LoopRun, PendingEscalation, SubAgentRun

logger = logging.getLogger("aether.loops.safety_loop")


class SafetyRiskType(str, enum.Enum):
    """The five §16.4 risk types (runtime classification; the string is
    stored in loop_runs.trigger per LOOP-04)."""

    RUNAWAY_LOOP = "runaway_loop"
    INVARIANT = "invariant"
    AUTHORITY = "authority"
    CONTRADICTION_CLUSTER = "contradiction_cluster"
    UNAPPROVED_ACTION = "unapproved_action"


# Optional injected coroutine awaited while executing a response — the
# extension point for a genuinely slow response (and the deterministic way
# to force the 10s fail-safe in tests). Signature: (loop_run, db) -> None.
ResponseHook = Callable[[LoopRun, AsyncSession], Awaitable[None]]


@dataclass
class SafetyOutcome:
    safety_loop_run_id: UUID
    risk_type: SafetyRiskType
    risk_neutralized: bool           # response completed within the 10s budget
    escalation_required: bool
    fail_safe_triggered: bool        # response exceeded 10s -> force-termination
    actions_taken: list[str] = field(default_factory=list)
    terminated_process_id: Optional[UUID] = None


class SafetyLoop:
    # max_duration_ms IS the real bound (10s per response). max_iterations
    # is a schema placeholder (see module docstring) — Safety has no
    # iteration limit by design.
    MAX_RESPONSE_MS: int = 10000
    PLACEHOLDER_MAX_ITERATIONS: int = 1

    async def run(
        self,
        risk_type: SafetyRiskType,
        context: dict,
        session_id: Optional[UUID],
        db: AsyncSession,
        *,
        response_hook: Optional[ResponseHook] = None,
    ) -> SafetyOutcome:
        # LOOP-04 logging line 2835: INSERT loop_runs(loop_type='safety',
        # trigger=risk_type) on every trigger. max_iterations placeholder=1
        # (config-vs-schema reconciliation, see module docstring).
        loop_run = LoopRun(
            loop_type=LoopType.SAFETY,
            trigger=risk_type.value,
            session_id=session_id,
            max_iterations=self.PLACEHOLDER_MAX_ITERATIONS,
            max_duration_ms=self.MAX_RESPONSE_MS,
            status=LoopStatus.RUNNING,
            iteration_count=1,
        )
        db.add(loop_run)
        await db.flush()

        outcome = SafetyOutcome(
            safety_loop_run_id=loop_run.id, risk_type=risk_type,
            risk_neutralized=False, escalation_required=True, fail_safe_triggered=False,
        )

        # Execute the spec'd risk response for this risk type.
        actions = await self._respond(risk_type, context, session_id, db)
        outcome.actions_taken.extend(actions)

        # Injection point: a genuinely slow response (or, in tests, a hook
        # that back-dates start_time to force the fail-safe deterministically).
        if response_hook is not None:
            await response_hook(loop_run, db)

        # LOOP-04 every_response: action_log(surface).
        if session_id is not None:
            db.add(ActionLog(
                session_id=session_id, agent=AgentName.MASTER, action_type=ActionType.SURFACE,
                output_summary=f"safety_loop: {risk_type.value} response — {'; '.join(actions)}"[:500],
            ))
        await db.flush()

        # Fail-safe (LOOP-04 lines 2828-2832): if the response took > 10s,
        # force-terminate the triggering loop/process and escalate p0. Err
        # toward stopping the affected process.
        elapsed_ms = self._elapsed_ms(loop_run)
        if elapsed_ms > self.MAX_RESPONSE_MS:
            terminated_id = await self._fail_safe_force_terminate(context, session_id, db)
            outcome.fail_safe_triggered = True
            outcome.risk_neutralized = False
            outcome.terminated_process_id = terminated_id
            outcome.actions_taken.append(
                f"fail_safe: response exceeded {self.MAX_RESPONSE_MS}ms; force-terminated {terminated_id}"
            )
            loop_run.status = LoopStatus.FORCED_TERMINATION
            loop_run.end_time = datetime.now(timezone.utc)
            loop_run.notes = f"safety_timeout after {int(elapsed_ms)}ms"
            await db.flush()
            logger.critical(
                "safety_loop: fail-safe fired for %s (%.0fms); force-terminated %s",
                risk_type.value, elapsed_ms, terminated_id,
            )
            return outcome

        # Response completed within budget.
        outcome.risk_neutralized = True
        loop_run.status = LoopStatus.COMPLETED
        loop_run.end_time = datetime.now(timezone.utc)
        loop_run.notes = f"{risk_type.value} neutralized"
        await db.flush()
        logger.info("safety_loop: %s neutralized (%d actions)", risk_type.value, len(actions))
        return outcome

    # ------------------------------------------------------------------ #
    # Risk responses (orchestrate existing enforcement; don't reinvent it)
    # ------------------------------------------------------------------ #

    async def _respond(
        self, risk_type: SafetyRiskType, context: dict, session_id: Optional[UUID], db: AsyncSession
    ) -> list[str]:
        dispatch = {
            SafetyRiskType.RUNAWAY_LOOP: self._respond_runaway,
            SafetyRiskType.INVARIANT: self._respond_invariant,
            SafetyRiskType.AUTHORITY: self._respond_authority,
            SafetyRiskType.CONTRADICTION_CLUSTER: self._respond_contradiction,
            SafetyRiskType.UNAPPROVED_ACTION: self._respond_unapproved,
        }
        return await dispatch[risk_type](context, session_id, db)

    async def _respond_runaway(self, context, session_id, db) -> list[str]:
        # §16.4: "Loop already terminated by watchdog." Log + p0 escalation.
        await self._escalate(db, session_id, PriorityClass.P0, "Runaway loop terminated",
                       context.get("detail", "A loop exceeded its bounds and was terminated by the watchdog."),
                       extra={"loop_run_id": _s(context.get("triggering_loop_run_id"))})
        return ["watchdog already terminated the runaway loop", "escalated p0 safety_alert"]

    async def _respond_invariant(self, context, session_id, db) -> list[str]:
        # §16.4: terminate all active sub-agents, suspend goal loop, p0, snapshot.
        actions: list[str] = []
        if session_id is not None:
            res = await db.execute(
                update(SubAgentRun)
                .where(SubAgentRun.session_id == session_id,
                       SubAgentRun.status.in_((SubAgentStatus.SPAWNED, SubAgentStatus.RUNNING)))
                .values(status=SubAgentStatus.FORCE_TERMINATED, terminated_at=datetime.now(timezone.utc))
            )
            actions.append(f"terminated {res.rowcount or 0} active sub-agents")
            res2 = await db.execute(
                update(LoopRun)
                .where(LoopRun.session_id == session_id, LoopRun.loop_type == LoopType.GOAL,
                       LoopRun.status == LoopStatus.RUNNING)
                .values(status=LoopStatus.FAILED, end_time=datetime.now(timezone.utc))
            )
            actions.append(f"suspended {res2.rowcount or 0} goal loop(s)")
            # Save session state snapshot.
            l1 = await rebuild_l1(session_id, db)
            await save_l1_snapshot(session_id, l1, db)
            actions.append("saved session state snapshot")
        await self._escalate(db, session_id, PriorityClass.P0, "Invariant violation — task suspended",
                       context.get("detail", "An invariant was violated; the affected task is suspended pending review."),
                       extra={"node_id": _s(context.get("node_id"))})
        actions.append("escalated p0 safety_alert")
        return actions

    async def _respond_authority(self, context, session_id, db) -> list[str]:
        # §16.4: gateway already blocked; log violation detail; p1; surface.
        if session_id is not None:
            db.add(ActionLog(
                session_id=session_id, agent=AgentName.MASTER, action_type=ActionType.SURFACE,
                output_summary=f"authority_violation blocked: {context.get('detail', '')}"[:500],
            ))
        await self._escalate(db, session_id, PriorityClass.P1, "Authority violation blocked",
                       context.get("detail", "An action beyond granted authority was blocked at the gateway."))
        return ["gateway already blocked the action", "logged violation detail", "escalated p1"]

    async def _respond_contradiction(self, context, session_id, db) -> list[str]:
        # §16.4: halt synthesis on affected pillar; p1 "Resolve contradictions".
        # No synthesis-suspension state machine exists in Phase-2, so "halt
        # synthesis" is realized as the escalation + surface note (documented).
        pillar = context.get("pillar", "unknown")
        await self._escalate(db, session_id, PriorityClass.P1, f"Contradiction cluster in {pillar}",
                       f"Resolve contradictions in {pillar}.", extra={"pillar": pillar})
        return [f"flagged contradiction cluster in {pillar} (synthesis halt noted)", "escalated p1"]

    async def _respond_unapproved(self, context, session_id, db) -> list[str]:
        # §16.4: gateway already blocked (INV-05); log blocked action; p1.
        if session_id is not None:
            db.add(ActionLog(
                session_id=session_id, agent=AgentName.MASTER, action_type=ActionType.SURFACE,
                output_summary=f"unapproved external action blocked: {context.get('detail', '')}"[:500],
            ))
        await self._escalate(db, session_id, PriorityClass.P1, "Unapproved external action blocked",
                       context.get("detail", "An external action without approval was blocked at the gateway (INV-05)."))
        return ["gateway already blocked (INV-05)", "logged blocked action", "escalated p1"]

    # ------------------------------------------------------------------ #

    async def _escalate(self, db, session_id, priority, title, description, *, extra=None) -> bool:
        """Insert a SAFETY_ALERT escalation. P0 inserts are de-duplicated.

        Bug found by the EC-29 fail-safe test: the P0 index
        ``uq_pe_p0_pending_per_node`` is ``NULLS NOT DISTINCT`` (migration
        0004), so a session may hold at most ONE node-less pending P0 at a
        time (PB-03 anti-spam). A runaway/invariant response escalates a
        node-less P0, and then the fail-safe tries to escalate a SECOND
        node-less P0 (safety_timeout) — a raw insert raised IntegrityError
        and crashed the fail-safe, the one path LOOP-04 says must never
        fail ("if safety loop itself fails, always err toward stopping the
        affected process"). Fix: pre-check (null-safe, matching the index's
        NULLS NOT DISTINCT semantics) and dedupe instead of colliding. When
        a node-less P0 already pends, the second collapses into it — the
        user still gets the P0 interrupt, and the collapsed signal is
        recorded in loop_run.notes + the action_log surface, not lost.
        Flagged for the HANDOFF as a LOOP-04-vs-schema reconciliation
        (LOOP-04 says "INSERT p0 safety_timeout"; the schema permits only
        one node-less pending P0 per session).
        """
        content = {"title": title, "description": description}
        if extra:
            content.update({k: v for k, v in extra.items() if v is not None})
        if priority == PriorityClass.P0 and session_id is not None:
            node_key = content.get("node_id")
            existing = (await db.execute(
                select(PendingEscalation.id).where(
                    PendingEscalation.session_id == session_id,
                    PendingEscalation.priority_class == PriorityClass.P0,
                    PendingEscalation.status == EscalationStatus.PENDING,
                    PendingEscalation.content["node_id"].astext.is_not_distinct_from(node_key),
                )
            )).first()
            if existing is not None:
                logger.warning(
                    "safety_loop: P0 escalation '%s' deduped (uq_pe_p0_pending_per_node "
                    "NULLS NOT DISTINCT: one node-less pending P0 per session)", title,
                )
                return False
        db.add(PendingEscalation(
            escalation_type=EscalationType.SAFETY_ALERT, priority_class=priority,
            content=content, session_id=session_id, status=EscalationStatus.PENDING,
        ))
        await db.flush()
        return True

    async def _fail_safe_force_terminate(
        self, context: dict, session_id: Optional[UUID], db: AsyncSession
    ) -> Optional[UUID]:
        """LOOP-04 fail-safe: force-terminate the triggering loop/process and
        escalate p0 safety_timeout. (force-terminate realized inline; the
        watchdog performs the same UPDATE for its own timeouts.)"""
        now = datetime.now(timezone.utc)
        terminated_id: Optional[UUID] = None

        trig_loop = context.get("triggering_loop_run_id")
        if trig_loop is not None:
            await db.execute(
                update(LoopRun)
                .where(LoopRun.id == trig_loop, LoopRun.status == LoopStatus.RUNNING)
                .values(status=LoopStatus.FORCED_TERMINATION, end_time=now)
            )
            terminated_id = trig_loop

        trig_sa = context.get("triggering_sub_agent_run_id")
        if trig_sa is not None:
            await db.execute(
                update(SubAgentRun)
                .where(SubAgentRun.id == trig_sa,
                       SubAgentRun.status.in_((SubAgentStatus.SPAWNED, SubAgentStatus.RUNNING)))
                .values(status=SubAgentStatus.FORCE_TERMINATED, terminated_at=now)
            )
            terminated_id = terminated_id or trig_sa

        # p0 safety_timeout (mapped to SAFETY_ALERT — see module docstring).
        # Routed through the dedup-aware _escalate: if the response already
        # queued a node-less P0 (runaway/invariant), this collapses into it
        # rather than colliding on uq_pe_p0_pending_per_node.
        await self._escalate(
            db, session_id, PriorityClass.P0, "Safety response timeout",
            "A safety response exceeded 10s; the triggering process was force-terminated.",
            extra={"safety_event": "safety_timeout"},
        )
        return terminated_id

    def _elapsed_ms(self, loop_run: LoopRun) -> float:
        start = loop_run.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - start).total_seconds() * 1000


def _s(v) -> Optional[str]:
    return str(v) if v is not None else None
