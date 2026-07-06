"""
tests.models.test_phase1_tables
=================================

Empirical verification of migration 0005's five agent-ecosystem tables
against the real Postgres instance, as the app role (aether_app_role) —
same standard Phase-0 held (HANDOFF.md, AETHER_PHASE1_PROMPT §6).

Covers, per the "check against a real running DB" rule:
  - each table round-trips a real INSERT via the restricted role;
  - INV-02: DELETE is denied to aether_app_role on every one of the
    five tables (nothing the app touches is ever hard-deleted);
  - sub_agent_runs supports the spawned -> terminal UPDATE the runtime
    depends on (mirrors loop_runs);
  - skill_chains' semver CHECK rejects a malformed version;
  - skill_performance's UPSERT-key UNIQUE constraint holds.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from aether.models.enums import SkillStatus, SubAgentStatus
from aether.models.runtime import (
    MetaLoopRun,
    SkillChain,
    SkillPerformance,
    SubAgent,
    SubAgentRun,
)


async def _make_sub_agent(db_session, name: str = "test.dummy_sub_agent") -> SubAgent:
    sa_row = SubAgent(
        name=name,
        parent_agent="legal",
        domain="legal",
        description="Scans legal nodes for upcoming deadlines.",
        trigger_event="session.opened",
        termination_condition="Returns deadline list to parent",
        max_duration_ms=30000,
        max_iterations=1,
        authority_level=0,
        phase_introduced=1,
    )
    db_session.add(sa_row)
    await db_session.commit()
    return sa_row


@pytest.mark.asyncio
async def test_sub_agents_insert_roundtrip(db_session):
    sa_row = await _make_sub_agent(db_session)
    assert sa_row.id is not None
    assert sa_row.status == "active"  # server default
    assert sa_row.created_at is not None


@pytest.mark.asyncio
async def test_sub_agent_runs_lifecycle_update(db_session, test_session_row):
    """spawned -> completed transition must be permitted (like loop_runs)."""
    sa_row = await _make_sub_agent(db_session, name="test.dummy_sub_agent.lifecycle")
    run = SubAgentRun(
        sub_agent_id=sa_row.id,
        session_id=test_session_row.id,
        parent_agent="legal",
    )
    db_session.add(run)
    await db_session.commit()
    assert run.status == SubAgentStatus.SPAWNED

    run.status = SubAgentStatus.COMPLETED
    run.result_summary = {"deadlines_found": 2}
    await db_session.commit()

    refetched = await db_session.get(SubAgentRun, run.id)
    assert refetched.status == SubAgentStatus.COMPLETED
    assert refetched.result_summary == {"deadlines_found": 2}


@pytest.mark.asyncio
async def test_skill_chains_valid_version_roundtrip(db_session):
    chain = SkillChain(
        name="chain.session_open",
        version="1.0.0",
        status=SkillStatus.ACTIVE,
        skill_sequence=[
            {"skill_name": "operational.session_initializer", "skill_version": "1.0.0",
             "input_binding": {}, "required": True},
        ],
        max_length=3,
    )
    db_session.add(chain)
    await db_session.commit()
    assert chain.id is not None
    assert chain.updated_at is not None


@pytest.mark.asyncio
async def test_skill_chains_bad_version_rejected(db_session):
    """The semver CHECK ('^[0-9]+[.][0-9]+[.][0-9]+$') must reject 'v1'."""
    chain = SkillChain(
        name="chain.bad",
        version="v1",  # not semver
        status=SkillStatus.DRAFT,
        skill_sequence=[],
    )
    db_session.add(chain)
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.commit()


@pytest.mark.asyncio
async def test_skill_performance_upsert_key_unique(db_session):
    from datetime import datetime, timezone

    ws = datetime(2026, 1, 1, tzinfo=timezone.utc)
    we = datetime(2026, 1, 31, tzinfo=timezone.utc)
    first = SkillPerformance(
        skill_name="cognitive.intent_parser", window_start=ws, window_end=we,
        invocation_count=10, accuracy_score=0.95, p95_latency_ms=800, error_rate=0.01,
    )
    db_session.add(first)
    await db_session.commit()

    dup = SkillPerformance(
        skill_name="cognitive.intent_parser", window_start=ws, window_end=we,
        invocation_count=99,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_meta_loop_runs_insert_roundtrip(db_session):
    mlr = MetaLoopRun(
        lookback_days=7,
        loop_health_scorecard={"goal": {"total_runs": 5, "completed": 5}},
        improvement_signals=["reflection loop latency trending up"],
        triggered_by="manual",
    )
    db_session.add(mlr)
    await db_session.commit()
    assert mlr.id is not None
    assert mlr.reviewed_by_user is False


# ---- INV-02: DELETE denied to aether_app_role on all five tables ------

@pytest.mark.parametrize(
    "table",
    ["sub_agents", "sub_agent_runs", "skill_chains", "skill_performance", "meta_loop_runs"],
)
@pytest.mark.asyncio
async def test_inv02_delete_denied_on_phase1_tables(db_session, table):
    with pytest.raises(DBAPIError):
        await db_session.execute(text(f"DELETE FROM {table}"))
