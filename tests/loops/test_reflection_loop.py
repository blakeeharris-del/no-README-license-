"""
tests.loops.test_reflection_loop
==================================

EC-21: the Reflection Loop runs after every session close following
§16.2's exact 6-step sequence, and a forced Reflection Loop failure
does not block the next session from opening (fail-safe verified by
test, not assumed).
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import select

from aether.loops.reflection_loop import ReflectionLoop
from aether.models.enums import LoopStatus, LoopType
from aether.models.runtime import SkillInvocationLog
from aether.models.runtime import LoopRun


# ---- unit: 6-step sequence (steps 1-3 always run) ---------------------

@pytest.mark.asyncio
async def test_reflection_loop_runs_sequence(db_session, test_session_row):
    loop_run = await ReflectionLoop().run(test_session_row.id, db_session)

    assert loop_run.loop_type == LoopType.REFLECTION
    assert loop_run.status == LoopStatus.COMPLETED
    assert loop_run.end_time is not None

    # Steps 1-3 ran via this loop_run (invoked with loop_run_id set).
    logged = set((await db_session.execute(
        select(SkillInvocationLog.skill_name).where(
            SkillInvocationLog.loop_run_id == loop_run.id
        )
    )).scalars().all())
    assert {"evaluative.memory_integrity_checker",
            "evaluative.confidence_auditor",
            "evaluative.skill_performance_tracker"} <= logged


# ---- unit: fail-safe (run() never raises) -----------------------------

@pytest.mark.asyncio
async def test_reflection_loop_fail_safe(db_session, test_session_row, monkeypatch):
    async def boom(self, *a, **k):
        raise RuntimeError("forced reflection failure")

    monkeypatch.setattr(ReflectionLoop, "_sequence", boom)

    # Must NOT raise despite the forced failure.
    loop_run = await ReflectionLoop().run(test_session_row.id, db_session)
    assert loop_run.status == LoopStatus.FAILED
    assert loop_run.end_time is not None


# ---- integration: forced failure does not block the next session ------

@pytest_asyncio.fixture
async def client():
    from aether.api.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.mark.asyncio
async def test_forced_reflection_failure_does_not_block_next_session(client, monkeypatch):
    # Force every Reflection Loop to fail internally.
    async def boom(self, *a, **k):
        raise RuntimeError("forced reflection failure")

    monkeypatch.setattr(ReflectionLoop, "_sequence", boom)

    r1 = await client.post("/session/start")
    assert r1.status_code == 200
    sid = r1.json()["session_id"]

    # Close triggers the (failing) Reflection Loop — must still return 200.
    rc = await client.post(f"/session/{sid}/close")
    assert rc.status_code == 200

    # The next session must open regardless of the reflection failure.
    r2 = await client.post("/session/start")
    assert r2.status_code == 200
    assert r2.json()["session_id"] != sid
