"""
aether.loops.shutdown_loop
============================

The Shutdown Loop (Implementation Plan §16.6). The graceful teardown:
on trigger it terminates every active process for a session — all
active ``loop_runs`` AND ``sub_agent_runs`` — in reverse spawn order
(children before parents), then preserves state and closes the session.
INV-09 (System State Is Always Recoverable) is the invariant it enforces.

Exact §16.6 execution sequence (implemented below step-for-step):
  STEP 1: Identify all active loop_runs and sub_agent_runs for this session
  STEP 2: For each active process (reverse spawn order):
            Signal shutdown_requested. Wait 10,000ms.
            Clean exit: UPDATE status=completed
            Timeout: watchdog.force_terminate(id) -> UPDATE status=force_terminated
  STEP 3: Serialize L1 to sessions.l1_snapshot
          Write uncommitted node writes as status=pending_review draft nodes
          Ensure pending_escalations rows committed
  STEP 4: UPDATE sessions: ended_at=now(), status=closed
          LLM call (temp=0.3) to generate session summary; UPDATE sessions.summary
  STEP 5: Emit session.closed event -> triggers reflection loop (async)
          Emit synthesis.threshold_check event

Reverse spawn order (the EC-32 property): active loop_runs (keyed on
``start_time``) and sub_agent_runs (keyed on ``spawned_at``) are merged
and processed newest-first. A child process is always spawned *after*
its parent (a sub-loop/sub-agent cannot exist before the loop that
spawned it), so descending spawn timestamp is exactly children-before-
parents. This is what closes the "reachable-for-termination != watched-
by-parent" gap: the ordinary session close only completes the top-level
goal loop, leaving nested Correction/Escalation loops and sub_agent_runs
still marked active; the Shutdown Loop reaches the whole tree.

STEP 2 deviation (documented, not silent): §16.6's "signal
shutdown_requested / wait 10,000ms / clean-exit-vs-timeout" protocol
presumes detached processes that respond to a shutdown signal within
10s. Phase-2 loops run inline (the same inline-execution reality
HANDOFF_PHASE1 documented for the Reflection Loop) — there is no live
coroutine to signal or await. So an active row at shutdown is
force-terminated: that is the honest status (it was cut off by shutdown,
not cleanly completed), and leaving it RUNNING would be exactly the
orphan INV-09 forbids. The clean-exit branch is left as the documented
extension point for a future detached-process runtime; no fake 10s wait
and no fake "clean exit" are fabricated here.

Self-exclusion: the Shutdown Loop records its own ``loop_runs`` row
(EC-29). STEP 1 explicitly excludes that row from the active set, or the
loop would force-terminate itself mid-run.

Bounds / EC-29: §16.6 has no parameters block and its sequence is a
single linear teardown, not an iterating loop — so this is a terminal
action, run once (max_iterations=1). It still records a bounded
``loop_runs`` row (SHUTDOWN, max_duration_ms=15000) so the SHUTDOWN
loop_type satisfies EC-29's "all seven produce real loop_runs entries
with bounds" and the watchdog can force-terminate a hung teardown.

Overlap with close_session (for the consolidation checkpoint, not
changed here): the existing /close endpoint already performs §16.6
STEP 3-5's equivalent (L1 snapshot, session close, summary, reflection
trigger) but NOT STEP 1-2. This loop is the spec-complete teardown;
wiring /close to delegate to it is a consolidation follow-up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.memory.session_state import rebuild_l1, save_l1_snapshot
from aether.models.enums import LoopStatus, LoopType, SessionStatus, SubAgentStatus
from aether.models.runtime import LoopRun, SubAgentRun
from aether.models.sessions import Session
from aether.schemas.session import L1WorkingMemory

logger = logging.getLogger("aether.loops.shutdown_loop")

_ACTIVE_SUBAGENT = (SubAgentStatus.SPAWNED, SubAgentStatus.RUNNING)

SummaryFn = Callable[[L1WorkingMemory], Awaitable[str]]


@dataclass
class TerminatedProcess:
    kind: str          # "loop" | "sub_agent"
    id: UUID
    spawn_ts: datetime
    terminal_status: str


@dataclass
class ShutdownOutcome:
    shutdown_loop_run_id: UUID
    terminated: list[TerminatedProcess] = field(default_factory=list)  # processing order
    session_closed: bool = False
    summary: Optional[str] = None


class ShutdownLoop:
    MAX_ITERATIONS: int = 1        # single-pass terminal action
    MAX_DURATION_MS: int = 15000   # teardown budget (documented; §16.6 states none)

    async def run(
        self,
        session_id: UUID,
        db: AsyncSession,
        *,
        trigger: str = "shutdown_requested",
        summary_fn: Optional[SummaryFn] = None,
        trigger_reflection: bool = True,
    ) -> ShutdownOutcome:
        # Record the Shutdown Loop's own bounded loop_runs row (EC-29).
        shutdown_run = LoopRun(
            loop_type=LoopType.SHUTDOWN,
            trigger=trigger,
            session_id=session_id,
            max_iterations=self.MAX_ITERATIONS,
            max_duration_ms=self.MAX_DURATION_MS,
            status=LoopStatus.RUNNING,
            iteration_count=1,
        )
        db.add(shutdown_run)
        await db.flush()
        shutdown_run_id = shutdown_run.id

        outcome = ShutdownOutcome(shutdown_loop_run_id=shutdown_run_id)

        # STEP 1-2: terminate the full active tree (self-excluded), reverse
        # spawn order. Extracted so /close can invoke it directly (closing
        # the INV-09 orphan gap) without duplicating STEP 3-5.
        outcome.terminated = await self.terminate_active_tree(
            session_id, db, exclude_loop_run_id=shutdown_run_id
        )
        now = datetime.now(timezone.utc)

        # STEP 3: preserve state. Serialize L1 to sessions.l1_snapshot and
        # ensure escalations are committed. There is no in-memory
        # uncommitted-write buffer in this architecture — node writes commit
        # synchronously through write_protocol — so the "uncommitted node
        # writes -> pending_review draft nodes" sub-step is a documented
        # no-op here (nothing is ever left uncommitted to drain).
        l1 = await rebuild_l1(session_id, db)
        await save_l1_snapshot(session_id, l1, db)

        # STEP 4: close the session and record a summary.
        session_row = (
            await db.execute(select(Session).where(Session.id == session_id))
        ).scalar_one_or_none()
        if session_row is not None and session_row.status != SessionStatus.CLOSED:
            session_row.status = SessionStatus.CLOSED
            session_row.ended_at = now
            summary = await self._summary(l1, summary_fn)
            session_row.summary = summary
            outcome.summary = summary
            outcome.session_closed = True
        await db.flush()

        # Mark the shutdown loop itself complete before triggering downstream
        # loops (so its own row is terminal and never an orphan).
        shutdown_run.status = LoopStatus.COMPLETED
        shutdown_run.end_time = datetime.now(timezone.utc)
        shutdown_run.notes = f"terminated {len(outcome.terminated)} processes"
        await db.flush()

        # STEP 5: emit session.closed -> reflection loop (async, fail-safe).
        # synthesis.threshold_check is the /close endpoint's existing
        # responsibility (documented overlap); not re-emitted here.
        if trigger_reflection:
            try:
                from aether.loops.reflection_loop import ReflectionLoop

                await ReflectionLoop().run(session_id, db, trigger="session.closed")
            except Exception:  # pragma: no cover - run() already guards
                logger.exception("shutdown_loop: reflection trigger failed (non-blocking)")

        return outcome

    async def _summary(self, l1: L1WorkingMemory, summary_fn: Optional[SummaryFn]) -> str:
        if summary_fn is not None:
            return await summary_fn(l1)
        return await self._default_summary(l1)

    async def terminate_active_tree(
        self,
        session_id: UUID,
        db: AsyncSession,
        *,
        exclude_loop_run_id: Optional[UUID] = None,
    ) -> list[TerminatedProcess]:
        """§16.6 STEP 1-2: identify and force-terminate every active
        ``loop_runs`` and ``sub_agent_runs`` for the session, in reverse
        spawn order (children before parents). ``exclude_loop_run_id`` omits
        the caller's own row (the Shutdown Loop's, when invoked from run()).

        Reusable entry point: ``/close`` calls this directly to reach the
        full nested tree (closing the INV-09 orphan gap) without duplicating
        STEP 3-5, which the endpoint already performs."""
        active_loops = (
            await db.execute(
                select(LoopRun).where(
                    LoopRun.session_id == session_id,
                    LoopRun.status == LoopStatus.RUNNING,
                    *([LoopRun.id != exclude_loop_run_id] if exclude_loop_run_id is not None else []),
                )
            )
        ).scalars().all()
        active_subagents = (
            await db.execute(
                select(SubAgentRun).where(
                    SubAgentRun.session_id == session_id,
                    SubAgentRun.status.in_(_ACTIVE_SUBAGENT),
                )
            )
        ).scalars().all()

        processes: list[tuple[datetime, str, object]] = []
        for loop in active_loops:
            processes.append((_aware(loop.start_time), "loop", loop))
        for sa in active_subagents:
            processes.append((_aware(sa.spawned_at), "sub_agent", sa))
        processes.sort(key=lambda p: p[0], reverse=True)

        now = datetime.now(timezone.utc)
        terminated: list[TerminatedProcess] = []
        for spawn_ts, kind, obj in processes:
            if kind == "loop":
                obj.status = LoopStatus.FORCED_TERMINATION
                obj.end_time = now
                terminal = LoopStatus.FORCED_TERMINATION.value
            else:
                obj.status = SubAgentStatus.FORCE_TERMINATED
                obj.terminated_at = now
                terminal = SubAgentStatus.FORCE_TERMINATED.value
            terminated.append(
                TerminatedProcess(kind=kind, id=obj.id, spawn_ts=spawn_ts, terminal_status=terminal)
            )
        await db.flush()
        logger.info(
            "shutdown: terminated %d active processes (reverse spawn order) for session %s",
            len(terminated), session_id,
        )
        return terminated

    async def _default_summary(self, l1: L1WorkingMemory) -> str:
        """Guarded LLM summary (temp=0.3), §16.6 STEP 4. Never blocks
        shutdown: any failure falls back to a generic line (mirrors the
        /close endpoint's behavior)."""
        import json

        import anthropic

        from aether.config import settings

        try:
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            response = await client.messages.create(
                model=settings.anthropic_model,
                max_tokens=256,
                temperature=0.3,
                system="Summarize this session's working memory state in 2-3 plain sentences for Blake.",
                messages=[{"role": "user", "content": json.dumps(l1.model_dump(mode="json"))}],
            )
            return "".join(block.text for block in response.content if block.type == "text")
        except Exception:
            logger.exception("shutdown_loop: summary LLM call failed; using fallback")
            return "Session shut down. Summary unavailable."


def _aware(ts: datetime) -> datetime:
    """Normalize a possibly-naive timestamp to UTC-aware for comparison."""
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
