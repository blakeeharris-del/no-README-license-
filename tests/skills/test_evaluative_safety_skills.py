"""
tests.skills.test_evaluative_safety_skills
============================================

Verifies the four evaluative skills (SKILL-26..29) via real
invoke_skill(), and the real rollback_executor (EC-27) via a direct
call (safety skills bypass invoke_skill). Feeds EC-20 (skill_performance)
and EC-24 (loop scorecard).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from aether.models.enums import (
    ConfidenceLevel, CreatedByAgent, LinkType, LoopStatus, LoopType, NodeSource,
    NodeStatus, NodeType, PillarName,
)
from aether.models.nodes import Node, NodeLink, NodePillar
from aether.models.runtime import LoopRun, PendingEscalation, SkillInvocationLog
from aether.skills.invoker import invoke_skill


async def _mknode(db, session_id, *, pillar=PillarName.LEGAL, with_pillar=True,
                  source=NodeSource.AGENT_WRITE, confidence=ConfidenceLevel.INFERRED,
                  status=NodeStatus.ACTIVE, metadata=None, title="n") -> Node:
    node = Node(type=NodeType.FACT, title=title, content="c", source=source,
                confidence=confidence, status=status,
                created_by=CreatedByAgent.USER if source == NodeSource.USER_EXPLICIT else CreatedByAgent.MASTER_AGENT,
                session_id=session_id, metadata_=metadata or {})
    db.add(node); await db.flush()
    if with_pillar:
        db.add(NodePillar(node_id=node.id, pillar=pillar, is_primary=True, assigned_by=CreatedByAgent.USER))
        await db.flush()
    return node


# ---- confidence_auditor (SKILL-26) ------------------------------------

@pytest.mark.asyncio
async def test_confidence_auditor_flags_overconfident(db_session, test_session_row):
    # inferred node with <3 support -> overconfident
    node = await _mknode(db_session, test_session_row.id,
                         confidence=ConfidenceLevel.INFERRED, metadata={"synthesis_from": []})
    r = await invoke_skill(
        "evaluative.confidence_auditor",
        {"session_id": str(test_session_row.id), "scope": "session", "synthesis_run_id": None},
        test_session_row.id, "master", None, db_session,
    )
    out = r.output
    assert out["nodes_reviewed"] >= 1
    assert any(v["violation_type"] == "overconfident" for v in out["violations"])
    assert out["flags_created"] >= 1
    refetched = await db_session.get(Node, node.id)
    assert refetched.status == NodeStatus.FLAGGED


# ---- skill_performance_tracker (SKILL-27, EC-20) ----------------------

@pytest.mark.asyncio
async def test_skill_performance_tracker_writes_records(db_session, test_session_row):
    # generate a real invocation in this session first
    await invoke_skill(
        "cognitive.confidence_scorer",
        {"node_draft": {"title": "x", "content": "c", "source": "agent_write"},
         "supporting_node_ids": [], "pillar": "legal"},
        test_session_row.id, "master", None, db_session,
    )
    r = await invoke_skill(
        "evaluative.skill_performance_tracker",
        {"session_id": str(test_session_row.id)},
        test_session_row.id, "master", None, db_session,
    )
    out = r.output
    assert out["performance_records_written"] >= 1
    # a real skill_performance row exists with real invocation data
    row = (await db_session.execute(
        select(SkillInvocationLog).where(
            SkillInvocationLog.skill_name == "cognitive.confidence_scorer",
            SkillInvocationLog.session_id == test_session_row.id,
        )
    )).first()
    assert row is not None


# ---- loop_health_checker (SKILL-28, EC-24) ----------------------------

@pytest.mark.asyncio
async def test_loop_health_checker_scorecard(db_session, test_session_row):
    db_session.add(LoopRun(loop_type=LoopType.GOAL, trigger="t", session_id=test_session_row.id,
                           status=LoopStatus.COMPLETED, iteration_count=3,
                           max_iterations=10, max_duration_ms=60000))
    db_session.add(LoopRun(loop_type=LoopType.GOAL, trigger="t", session_id=test_session_row.id,
                           status=LoopStatus.FORCED_TERMINATION, iteration_count=10,
                           max_iterations=10, max_duration_ms=60000))
    await db_session.flush()
    r = await invoke_skill(
        "evaluative.loop_health_checker", {"lookback_days": 7},
        test_session_row.id, "master", None, db_session,
    )
    out = r.output
    goal = out["scorecard"]["goal"]
    # loop_health_checker aggregates ALL loop_runs in the window (no session
    # filter, per spec), so other tests' committed runs may be present;
    # assert this test's own contribution is included.
    assert goal["total_runs"] >= 2 and goal["forced_termination"] >= 1
    assert any(a["loop_type"] == "goal" and a["severity"] == "critical" for a in out["anomalies"])


# ---- memory_integrity_checker (SKILL-29) ------------------------------

@pytest.mark.asyncio
async def test_memory_integrity_checker_repairs_orphan(db_session, test_session_row):
    orphan = await _mknode(db_session, test_session_row.id, with_pillar=False, title="orphan")
    r = await invoke_skill(
        "evaluative.memory_integrity_checker",
        {"session_id": str(test_session_row.id), "full_scan": False},
        test_session_row.id, "master", None, db_session,
    )
    out = r.output
    assert out["orphaned_nodes"] >= 1
    assert any(v["violation_type"] == "missing_pillar" for v in out["violations"])
    # repaired: now has a relationships pillar row
    pillars = (await db_session.execute(
        select(NodePillar).where(NodePillar.node_id == orphan.id)
    )).scalars().all()
    assert any(p.pillar == PillarName.RELATIONSHIPS for p in pillars)


# ---- rollback_executor (EC-27, direct call) ---------------------------

@pytest.mark.asyncio
async def test_rollback_executor_archives_and_restores(db_session, test_session_row):
    from aether.skills.safety.rollback_executor import rollback_executor

    superseded = await _mknode(db_session, test_session_row.id,
                               status=NodeStatus.SUPERSEDED, title="old belief")
    superseding = await _mknode(db_session, test_session_row.id,
                                status=NodeStatus.ACTIVE, title="new belief")
    db_session.add(NodeLink(source_id=superseding.id, target_id=superseded.id,
                            link_type=LinkType.SUPERSEDES, created_by=CreatedByAgent.MASTER_AGENT))
    await db_session.flush()

    await rollback_executor(superseding.id, db_session,
                            session_id=test_session_row.id, reason="bad write")

    # archived (never deleted), superseded node restored, escalation raised
    assert (await db_session.get(Node, superseding.id)).status == NodeStatus.ARCHIVED
    assert (await db_session.get(Node, superseded.id)).status == NodeStatus.ACTIVE
    escs = (await db_session.execute(
        select(PendingEscalation).where(PendingEscalation.session_id == test_session_row.id)
    )).scalars().all()
    assert any((e.content or {}).get("node_id") == str(superseding.id) for e in escs)
    # link is NOT removed (INV-02 spirit: only status changes)
    link = (await db_session.execute(
        select(NodeLink).where(NodeLink.source_id == superseding.id)
    )).scalar_one_or_none()
    assert link is not None
