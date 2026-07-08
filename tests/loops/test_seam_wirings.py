"""
tests.loops.test_seam_wirings — Unit C (two approved seam-wirings).

1. /close -> Shutdown Loop STEP 1-2: session close terminates any active
   nested tree (sub-loops + sub_agent_runs) so nothing is orphaned at close
   (INV-09). Verified via the real /close endpoint on the real row states.
2. Correction STEP 6 -> Escalation auto-drain: the correction_exhaust row
   Correction writes is drained by the Escalation Loop automatically, with
   the accurate retries count carried through (the EC-31 cross-loop seam).
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import select

from aether.api.main import app
from aether.database import AsyncSessionLocal
from aether.loops.correction_loop import CorrectionErrorType, CorrectionLoop
from aether.models.enums import (
    EscalationType,
    LoopStatus,
    LoopType,
    SubAgentStatus,
)
from aether.models.runtime import LoopRun, PendingEscalation, SubAgent, SubAgentRun


@pytest_asyncio.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ---------------------------------------------------------------------------
# Seam 1: /close -> Shutdown STEP 1-2 (no orphaned nested tree)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_terminates_active_nested_tree(client):
    r = await client.post("/session/start")
    session_id = r.json()["session_id"]

    # Seed an active nested tree on the started session: a nested Correction
    # loop and a sub_agent_run, both active. (The goal loop was started by
    # /session/start.)
    async with AsyncSessionLocal() as db:
        goal = (await db.execute(
            select(LoopRun).where(LoopRun.session_id == session_id,
                                  LoopRun.loop_type == LoopType.GOAL)
        )).scalars().first()
        corr = LoopRun(
            loop_type=LoopType.CORRECTION, trigger="nested", session_id=session_id,
            parent_loop_run_id=goal.id, status=LoopStatus.RUNNING, iteration_count=1,
            max_iterations=3, max_duration_ms=180000,
        )
        db.add(corr)
        # Reuse an existing seeded catalog sub-agent (avoids committing a
        # fixed-name row that would collide on re-run of this client test).
        sa_id = (await db.execute(select(SubAgent.id).limit(1))).scalars().first()
        sub_run = SubAgentRun(sub_agent_id=sa_id, session_id=session_id,
                              parent_agent="legal", status=SubAgentStatus.SPAWNED)
        db.add(sub_run)
        await db.commit()
        corr_id, sub_id = corr.id, sub_run.id

    # Close via the real endpoint.
    rc = await client.post(f"/session/{session_id}/close")
    assert rc.status_code == 200

    # The nested tree is terminated — no orphans (INV-09), asserted on rows.
    # (The endpoint completes whichever RUNNING loop it treats as "active";
    #  terminate_active_tree force-terminates the rest. The seam's guarantee
    #  is that NOTHING is left active — not which specific row was completed.)
    async with AsyncSessionLocal() as db:
        corr_after = (await db.execute(select(LoopRun).where(LoopRun.id == corr_id))).scalar_one()
        sub_after = (await db.execute(select(SubAgentRun).where(SubAgentRun.id == sub_id))).scalar_one()
        terminal_loop = {LoopStatus.COMPLETED, LoopStatus.FORCED_TERMINATION, LoopStatus.FAILED}
        assert corr_after.status in terminal_loop                 # no longer RUNNING
        assert sub_after.status == SubAgentStatus.FORCE_TERMINATED  # only shutdown touches sub-agents
        # zero orphans for this session (the INV-09 property)
        running = (await db.execute(select(LoopRun).where(
            LoopRun.session_id == session_id, LoopRun.status == LoopStatus.RUNNING))).scalars().all()
        active_subs = (await db.execute(select(SubAgentRun).where(
            SubAgentRun.session_id == session_id,
            SubAgentRun.status.in_((SubAgentStatus.SPAWNED, SubAgentStatus.RUNNING))))).scalars().all()
        assert running == [] and active_subs == []


# ---------------------------------------------------------------------------
# Seam 2: Correction STEP 6 -> Escalation auto-drain (accurate retries flow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correction_auto_drains_to_escalation_with_accurate_retries(db_session, test_session_row):
    parent = LoopRun(
        loop_type=LoopType.GOAL, trigger="t", session_id=test_session_row.id,
        status=LoopStatus.RUNNING, iteration_count=0, max_iterations=10, max_duration_ms=120000,
    )
    db_session.add(parent)
    await db_session.commit()

    async def always_fail(attempt, error_type):
        return False

    outcome = await CorrectionLoop().run(
        error_type=CorrectionErrorType.SKILL_FAILURE,
        parent_loop_run_id=parent.id, session_id=test_session_row.id,
        db=db_session, retry_fn=always_fail,
    )
    assert outcome.retries == 3

    # The correction_exhaust row was drained by the Escalation Loop: it is
    # enriched in place with a surfaced escalation_text carrying the accurate
    # retries count (the EC-31 seam) — end to end, no manual handoff.
    row = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.session_id == test_session_row.id,
            PendingEscalation.escalation_type == EscalationType.CORRECTION_EXHAUST,
        )
    )).scalars().one()
    assert row.content["retries"] == 3
    assert "escalation_text" in row.content          # Escalation Loop enriched it
    assert "3 retries" in row.content["escalation_text"]

    # An ESCALATION loop_run was produced (the drain actually ran).
    esc_runs = (await db_session.execute(
        select(LoopRun).where(
            LoopRun.session_id == test_session_row.id, LoopRun.loop_type == LoopType.ESCALATION)
    )).scalars().all()
    assert len(esc_runs) >= 1
