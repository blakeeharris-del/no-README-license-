"""
aether.loops.escalation_loop
==============================

The Escalation Loop (Implementation Plan §16.5). Bridges autonomous
operation and user judgment: one attempt per trigger, surfacing a signal
to the user (P0 interrupts immediately; everything else waits for the
next session brief). Unresponded items stay in ``pending_escalations``.

Exact §16.5 execution sequence (implemented below step-for-step):
  STEP 1: Classify: p0_signal | correction_exhaust | safety_alert | clarification
  STEP 2: cognitive.signal_scorer confirms P-class (prevents P0 inflation)
  STEP 3: executive.approval_presenter formats escalation text
          (what happened, why it matters, recommended action, options)
  STEP 4: INSERT pending_escalations row + INSERT action_log(surface)
  STEP 5: Surface in next AgentResponse (P0: interrupt immediately)
  TERMINATE: loop done; unresponded items stay queued for next session.

Bounds: §16.5's table gives no max_iterations/max_duration_ms (unlike
Goal/Correction). "One attempt per trigger" fixes max_iterations = 1;
max_duration_ms is set to 10,000 (documented choice — §16.5 states no
value, and 10s matches the system's §16.4 safety-response envelope while
staying well inside EC-31's 60s P0 requirement). Enforced by
``assert_loop_within_bounds`` (INV-08) and, externally, by the
LoopWatchdog on the ``loop_runs`` row.

STEP 2 anti-inflation rule (the point of "confirms P-class / prevents P0
inflation"): the signal_scorer is a CEILING on urgency for *claimed*
priorities — a p0_signal is only surfaced as P0 if the scorer
independently scores P0; otherwise it is downgraded to the scored class.
System-authoritative escalation types carry a mandated floor the scorer
cannot lower: correction_exhaust is always P1 (§16.3), safety_alert
always P0 (§16.4). This prevents both inflation (claims can't exceed the
scorer) and erroneous suppression (loop-internal signals keep their
mandated class — a "loop_event" scores P3 under signal_scorer, which
must not silently drop a correction exhaustion to P3).

STEP 4 P0 de-duplication: the schema's ``uq_pe_p0_pending_per_node``
partial-unique index (one pending P0 per node per session) is the
concurrency backstop. This loop additionally pre-checks (Missing Specs
SKILL-19 pattern, "check pending_escalations WHERE content->>'node_id'
... prevent duplicates") so a repeat P0 for the same node is recognized
and skipped rather than raising an IntegrityError that would poison the
transaction (PB-03: one notice per condition).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.invariants.guards import assert_loop_within_bounds
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
from aether.skills.invoker import invoke_skill

logger = logging.getLogger("aether.loops.escalation_loop")

# Rank: lower = more urgent. Used for the anti-inflation ceiling.
_PRIORITY_RANK = {
    PriorityClass.P0: 0,
    PriorityClass.P1: 1,
    PriorityClass.P2: 2,
    PriorityClass.P3: 3,
    PriorityClass.SUPPRESS: 4,
}

# Escalation types whose priority is system-mandated (scorer cannot lower).
_MANDATED_FLOOR = {
    EscalationType.CORRECTION_EXHAUST: PriorityClass.P1,
    EscalationType.SAFETY_ALERT: PriorityClass.P0,
}

_RISK_BY_PRIORITY = {
    PriorityClass.P0: "high",
    PriorityClass.P1: "medium",
    PriorityClass.P2: "low",
    PriorityClass.P3: "low",
    PriorityClass.SUPPRESS: "low",
}


@dataclass
class EscalationSignal:
    escalation_type: EscalationType
    claimed_priority: PriorityClass
    content: dict  # at minimum {"title", "description"}; may carry node_id, retries, ...
    # signal_scorer inputs (type/pillar/source/days_until/amount). Defaults to a
    # generic loop_event from the system when the caller supplies nothing.
    scoring_inputs: dict = field(default_factory=dict)


@dataclass
class EscalationOutcome:
    escalation_loop_run_id: UUID
    escalation_id: Optional[UUID]
    priority_class: PriorityClass
    escalation_text: str
    interrupt: bool          # STEP 5: P0 interrupts immediately
    deduped: bool            # STEP 4: an equivalent pending P0 already existed
    status: str              # "surfaced" | "deduped" | "suppressed"


class EscalationLoop:
    MAX_ITERATIONS: int = 1        # §16.5: one attempt per trigger
    MAX_DURATION_MS: int = 10000   # documented choice (see module docstring)

    async def run(
        self,
        signal: EscalationSignal,
        session_id: UUID,
        db: AsyncSession,
        *,
        existing_escalation_id: Optional[UUID] = None,
    ) -> EscalationOutcome:
        # Create the escalation loop_runs row (EC-29): one bounded row per
        # trigger, whatever the outcome.
        loop_run = LoopRun(
            loop_type=LoopType.ESCALATION,
            trigger=f"escalation:{signal.escalation_type.value}",
            session_id=session_id,
            max_iterations=self.MAX_ITERATIONS,
            max_duration_ms=self.MAX_DURATION_MS,
            status=LoopStatus.RUNNING,
            iteration_count=0,
        )
        db.add(loop_run)
        await db.flush()
        loop_run_id = loop_run.id

        # STEP 1: classify. The signal already carries its escalation_type
        # (one of the four §16.5 classes); nothing to re-derive.

        # INV-08 bounds check for this single attempt, then count it.
        start_time = loop_run.start_time
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        elapsed_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        assert_loop_within_bounds(
            loop_run.iteration_count, self.MAX_ITERATIONS, elapsed_ms, self.MAX_DURATION_MS,
            "escalation_loop", "EscalationLoop.run",
        )
        loop_run.iteration_count = 1
        await db.flush()

        # STEP 2: signal_scorer confirms the P-class (anti-inflation).
        scored = await self._score(signal, session_id, loop_run_id, db)
        priority = self._confirm_priority(signal, scored)

        if priority == PriorityClass.SUPPRESS:
            # Nothing to surface — the scorer judged this below the noise
            # floor and no mandated floor applies. Terminate cleanly.
            loop_run.status = LoopStatus.COMPLETED
            loop_run.end_time = datetime.now(timezone.utc)
            loop_run.notes = "suppressed by signal_scorer"
            await db.flush()
            return EscalationOutcome(
                escalation_loop_run_id=loop_run_id, escalation_id=None,
                priority_class=priority, escalation_text="", interrupt=False,
                deduped=False, status="suppressed",
            )

        # STEP 3: approval_presenter formats the escalation text.
        escalation_text = await self._format(signal, priority, session_id, loop_run_id, db)

        # STEP 4: write the pending_escalations row (or enrich the queued
        # one being consumed) + an action_log surface entry.
        escalation_id, deduped = await self._surface_row(
            signal, priority, escalation_text, session_id, loop_run_id, db,
            existing_escalation_id=existing_escalation_id,
        )

        # STEP 5: surface. P0 interrupts immediately; others wait for the
        # next session brief. The action_log surface row is the durable
        # "surfaced" artifact; the interrupt flag drives the goal loop.
        interrupt = priority == PriorityClass.P0 and not deduped

        loop_run.status = LoopStatus.COMPLETED
        loop_run.end_time = datetime.now(timezone.utc)
        loop_run.notes = f"surfaced {signal.escalation_type.value} as {priority.value}" + (
            " (deduped)" if deduped else ""
        )
        await db.flush()

        logger.info(
            "escalation_loop: %s surfaced as %s (interrupt=%s, deduped=%s)",
            signal.escalation_type.value, priority.value, interrupt, deduped,
        )
        return EscalationOutcome(
            escalation_loop_run_id=loop_run_id, escalation_id=escalation_id,
            priority_class=priority, escalation_text=escalation_text,
            interrupt=interrupt, deduped=deduped,
            status="deduped" if deduped else "surfaced",
        )

    async def run_for_pending(
        self, escalation_id: UUID, session_id: UUID, db: AsyncSession
    ) -> EscalationOutcome:
        """Consume an already-queued pending_escalations row (e.g. the
        correction_exhaust row the Correction Loop wrote). Builds the signal
        from that row and enriches it in place rather than duplicating."""
        row = (
            await db.execute(select(PendingEscalation).where(PendingEscalation.id == escalation_id))
        ).scalar_one()
        content = dict(row.content or {})
        signal = EscalationSignal(
            escalation_type=row.escalation_type,
            claimed_priority=row.priority_class,
            content=content,
            scoring_inputs={
                "type": "loop_event",
                "content": content.get("title", ""),
                "source": "watchdog" if row.escalation_type == EscalationType.SAFETY_ALERT else "system",
            },
        )
        return await self.run(signal, session_id, db, existing_escalation_id=escalation_id)

    # ------------------------------------------------------------------ #

    async def _score(
        self, signal: EscalationSignal, session_id: UUID, loop_run_id: UUID, db: AsyncSession
    ) -> PriorityClass:
        scoring_inputs = dict(signal.scoring_inputs) if signal.scoring_inputs else {}
        scoring_inputs.setdefault("type", "loop_event")
        scoring_inputs.setdefault("content", signal.content.get("title", ""))
        scoring_inputs.setdefault("source", "system")
        result = await invoke_skill(
            "cognitive.signal_scorer", {"signal": scoring_inputs},
            session_id, "escalation_loop", loop_run_id, db,
        )
        if result.status != "ok" or not result.output:
            # Scorer failure must not silently drop the signal: fall back to
            # the claimed priority (STEP 2 confirms, it does not gate).
            logger.warning("escalation_loop: signal_scorer returned %s; using claimed priority",
                           result.status)
            return signal.claimed_priority
        return PriorityClass(result.output["priority_class"])

    def _confirm_priority(self, signal: EscalationSignal, scored: PriorityClass) -> PriorityClass:
        """STEP 2 confirmation. Mandated-floor types keep their fixed class;
        claim-based types are capped at the scorer's assessment (the scorer
        can only downgrade, never inflate)."""
        floor = _MANDATED_FLOOR.get(signal.escalation_type)
        if floor is not None:
            return floor
        # Less urgent of {claimed, scored} = higher rank. Prevents inflation.
        less_urgent_rank = max(_PRIORITY_RANK[signal.claimed_priority], _PRIORITY_RANK[scored])
        for pclass, rank in _PRIORITY_RANK.items():
            if rank == less_urgent_rank:
                return pclass
        return scored

    async def _format(
        self, signal: EscalationSignal, priority: PriorityClass,
        session_id: UUID, loop_run_id: UUID, db: AsyncSession,
    ) -> str:
        content = signal.content
        # For correction_exhaust, surface the ACCURATE retries count the
        # Correction Loop recorded (the value the mid-retry bug fix
        # corrected) — this is the seam between the two loops.
        if signal.escalation_type == EscalationType.CORRECTION_EXHAUST:
            retries = content.get("retries")
            consequence = f"Correction exhausted after {retries} retries; parent loop failed and was rolled back."
        else:
            consequence = content.get("description", "")
        result = await invoke_skill(
            "executive.approval_presenter",
            {
                "action": content.get("title", "Escalation"),
                "target": content.get("description", ""),
                "amount_or_consequence": consequence,
                "timing": "immediate" if priority == PriorityClass.P0 else "next session brief",
                "risk_level": _RISK_BY_PRIORITY[priority],
            },
            session_id, "escalation_loop", loop_run_id, db,
        )
        if result.status == "ok" and result.output:
            return result.output["approval_text"]
        return f"{content.get('title', 'Escalation')}: {consequence}"

    async def _surface_row(
        self, signal: EscalationSignal, priority: PriorityClass, escalation_text: str,
        session_id: UUID, loop_run_id: UUID, db: AsyncSession,
        *, existing_escalation_id: Optional[UUID],
    ) -> tuple[UUID, bool]:
        node_id = signal.content.get("node_id")

        # STEP 4 P0 de-duplication pre-check: if a pending P0 already exists
        # for this (session, node_id), do not insert a second — recognize
        # the duplicate and return the existing row (PB-03).
        if priority == PriorityClass.P0 and node_id is not None and existing_escalation_id is None:
            existing = (
                await db.execute(
                    select(PendingEscalation).where(
                        PendingEscalation.session_id == session_id,
                        PendingEscalation.priority_class == PriorityClass.P0,
                        PendingEscalation.status == EscalationStatus.PENDING,
                        PendingEscalation.content["node_id"].astext == str(node_id),
                    )
                )
            ).scalars().first()
            if existing is not None:
                logger.info("escalation_loop: duplicate P0 for node %s — deduped", node_id)
                return existing.id, True

        enriched = dict(signal.content)
        enriched["escalation_text"] = escalation_text

        if existing_escalation_id is not None:
            # Consuming a queued row (e.g. correction_exhaust): enrich in
            # place, don't duplicate. Reassign content so JSONB change is
            # tracked.
            row = (
                await db.execute(
                    select(PendingEscalation).where(PendingEscalation.id == existing_escalation_id)
                )
            ).scalar_one()
            row.priority_class = priority
            row.content = enriched
            escalation_id = row.id
        else:
            row = PendingEscalation(
                escalation_type=signal.escalation_type,
                priority_class=priority,
                content=enriched,
                session_id=session_id,
                loop_run_id=loop_run_id,
                status=EscalationStatus.PENDING,
            )
            db.add(row)
            await db.flush()
            escalation_id = row.id

        # STEP 4: action_log surface entry (the durable "surfaced" record).
        db.add(
            ActionLog(
                session_id=session_id,
                agent=AgentName.MASTER,
                action_type=ActionType.SURFACE,
                output_summary=f"escalation surfaced: {signal.escalation_type.value} ({priority.value})"[:500],
            )
        )
        await db.flush()
        return escalation_id, False
