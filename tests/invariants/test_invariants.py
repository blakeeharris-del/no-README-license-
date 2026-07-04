"""
tests.invariants.test_invariants
====================================

Section 23: "Run invariant tests FIRST in CI. All must pass before any
other tests run."
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text

from aether.invariants.guards import (
    ConfidenceViolationError,
    InvariantViolation,
    LoopLimitExceeded,
    NodeValidationError,
    SkillExecutionError,
    SkillTimeoutError,
    WatchdogNotRunningError,
    assert_loop_within_bounds,
    assert_no_hard_delete,
)
from aether.loops.goal_loop import GoalLoop
from aether.loops.watchdog import LoopWatchdog
from aether.models.enums import (
    ConfidenceLevel,
    CreatedByAgent,
    LoopStatus,
    LoopType,
    NodeSource,
    NodeStatus,
    PillarName,
)
from aether.models.logs import ActionLog
from aether.models.nodes import Node, NodeLink
from aether.models.runtime import LoopRun
from aether.schemas.nodes import NodeDraft

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# INV-01
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inv01_log_row_committed_before_action(db_session, test_session_row, monkeypatch):
    from aether.memory.write_protocol import write_node

    draft = NodeDraft(
        type="fact", title="INV-01 test", content="c", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
        pillars=[PillarName.LEGAL], primary_pillar=PillarName.LEGAL,
    )
    result = await write_node(draft, test_session_row.id, "master", db_session)

    log_row = (
        await db_session.execute(
            select(ActionLog).where(ActionLog.node_ids.any(result.node_id))
        )
    ).scalar_one()
    node_row = (await db_session.execute(select(Node).where(Node.id == result.node_id))).scalar_one()
    assert log_row.action_type.value == "write"
    assert log_row.timestamp <= node_row.updated_at


# ---------------------------------------------------------------------------
# INV-02
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inv02_delete_on_nodes_raises_permission_error(db_session):
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        await db_session.execute(text("DELETE FROM nodes"))
    await db_session.rollback()

    with pytest.raises(InvariantViolation):
        assert_no_hard_delete("nodes", "DELETE FROM nodes", "test")


# ---------------------------------------------------------------------------
# INV-03
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inv03_agent_write_with_explicit_confidence_rejected(db_session, test_session_row):
    from aether.memory.write_protocol import write_node

    draft = NodeDraft(
        type="fact", title="bad", content="c", source=NodeSource.AGENT_WRITE,
        confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.MASTER_AGENT,
        pillars=[PillarName.LEGAL], primary_pillar=PillarName.LEGAL,
    )
    with pytest.raises(ConfidenceViolationError):
        await write_node(draft, test_session_row.id, "master", db_session)

    count = (await db_session.execute(select(Node))).scalars().all()
    assert len(count) == 0


@pytest.mark.asyncio
async def test_inv03_user_write_with_explicit_confidence_succeeds(db_session, test_session_row, monkeypatch):
    from aether.memory.write_protocol import write_node

    draft = NodeDraft(
        type="fact", title="good", content="c", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
        pillars=[PillarName.LEGAL], primary_pillar=PillarName.LEGAL,
    )
    result = await write_node(draft, test_session_row.id, "master", db_session)
    node = (await db_session.execute(select(Node).where(Node.id == result.node_id))).scalar_one()
    assert node.confidence == ConfidenceLevel.EXPLICIT


@pytest.mark.asyncio
async def test_inv03_agent_with_user_explicit_source_rejected(db_session, test_session_row):
    from aether.memory.write_protocol import write_node

    draft = NodeDraft(
        type="fact", title="forged", content="c", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.INFERRED, created_by=CreatedByAgent.MASTER_AGENT,
        pillars=[PillarName.LEGAL], primary_pillar=PillarName.LEGAL,
    )
    with pytest.raises(NodeValidationError):
        await write_node(draft, test_session_row.id, "master", db_session)
    count = (await db_session.execute(select(Node))).scalars().all()
    assert len(count) == 0


# ---------------------------------------------------------------------------
# INV-04
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inv04_speculative_pending_review_excluded_from_context(db_session, test_session_row):
    node = Node(
        type="fact", title="Speculative pending", content="c", source=NodeSource.AGENT_WRITE,
        confidence=ConfidenceLevel.SPECULATIVE, status=NodeStatus.PENDING_REVIEW,
        created_by=CreatedByAgent.MASTER_AGENT, session_id=test_session_row.id, metadata_={},
    )
    db_session.add(node)
    await db_session.flush()
    from aether.models.nodes import NodePillar

    db_session.add(NodePillar(node_id=node.id, pillar=PillarName.LEGAL, is_primary=True, assigned_by=CreatedByAgent.MASTER_AGENT))
    await db_session.commit()

    from aether.skills.operational.context_assembler import assemble_context

    intent = {"raw_input": "x", "action_type": "query", "subject": "x", "implied_pillars": ["legal"],
              "urgency": "standard", "entities": [], "ambiguity_flag": False}
    packet = await assemble_context({"intent": intent, "session_id": test_session_row.id}, db_session)
    node_ids_in_packet = [n["id"] for n in packet["relevant_nodes"]["nodes"]]
    assert str(node.id) not in node_ids_in_packet


@pytest.mark.asyncio
async def test_inv04_speculative_active_can_appear_in_contradiction_scan(db_session, test_session_row):
    from aether.models.nodes import NodePillar

    n1 = Node(type="fact", title="A", content="a", source=NodeSource.AGENT_WRITE, confidence=ConfidenceLevel.SPECULATIVE,
              status=NodeStatus.ACTIVE, created_by=CreatedByAgent.MASTER_AGENT, session_id=test_session_row.id, metadata_={})
    n2 = Node(type="fact", title="B", content="b", source=NodeSource.AGENT_WRITE, confidence=ConfidenceLevel.SPECULATIVE,
              status=NodeStatus.ACTIVE, created_by=CreatedByAgent.MASTER_AGENT, session_id=test_session_row.id, metadata_={})
    db_session.add_all([n1, n2])
    await db_session.flush()
    db_session.add(NodeLink(source_id=n1.id, target_id=n2.id, link_type="contradicts", created_by=CreatedByAgent.MASTER_AGENT))
    await db_session.commit()

    from aether.memory.read_protocol import scan_contradictions

    results = await scan_contradictions(db_session)
    pairs = [(r["node_id_a"], r["node_id_b"]) for r in results]
    assert (str(n1.id), str(n2.id)) in pairs


# ---------------------------------------------------------------------------
# INV-05
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inv05_gateway_blocked_without_user_confirmed_log_entry(db_session, test_session_row):
    from aether.skills.operational.action_gateway import action_gateway_skill

    result = await action_gateway_skill(
        {"action_type": "write", "target": "x", "payload": {}, "authority_level": 3,
         "session_id": str(test_session_row.id), "requesting_agent": "master"},
        db_session,
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "no_approval"


@pytest.mark.asyncio
async def test_inv05_gateway_passes_with_confirmed_entry_in_t0(db_session, test_session_row, settings_override):
    from aether.models.enums import ActionType, AgentName

    db_session.add(ActionLog(session_id=test_session_row.id, agent=AgentName.MASTER, action_type=ActionType.CONFIRM, user_confirmed=True))
    await db_session.commit()

    from aether.skills.operational.action_gateway import action_gateway_skill

    result = await action_gateway_skill(
        {"action_type": "write", "target": "x", "payload": {}, "authority_level": 3,
         "session_id": str(test_session_row.id), "requesting_agent": "master"},
        db_session,
    )
    assert result["status"] == "mock_executed"


# ---------------------------------------------------------------------------
# INV-06
# ---------------------------------------------------------------------------


def test_inv06_user_confirmed_only_set_by_approve_endpoint():
    """
    AST-based (not naive text search) so that documentation/comments
    which merely *mention* "user_confirmed=True" while explaining this
    very invariant (as several docstrings in this codebase do) aren't
    false-positived as violations. Looks specifically for either a
    keyword argument ``user_confirmed=True`` in a call, or an attribute
    assignment ``x.user_confirmed = True``.
    """

    def _is_true_literal(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and node.value is True

    violations = []
    for directory in ("agents", "skills"):
        for path in (_REPO_ROOT / "aether" / directory).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "user_confirmed" and _is_true_literal(kw.value):
                            violations.append(str(path))
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr == "user_confirmed" and _is_true_literal(node.value):
                            violations.append(str(path))
    assert violations == [], f"user_confirmed=True set outside api/routes/session.py: {violations}"


# ---------------------------------------------------------------------------
# INV-07
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inv07_contradiction_flags_both_nodes_and_creates_link(db_session, test_session_row):
    n1 = Node(type="fact", title="A", content="a", source=NodeSource.USER_EXPLICIT, confidence=ConfidenceLevel.EXPLICIT,
              status=NodeStatus.ACTIVE, created_by=CreatedByAgent.USER, session_id=test_session_row.id, metadata_={})
    n2 = Node(type="fact", title="B", content="b", source=NodeSource.USER_EXPLICIT, confidence=ConfidenceLevel.EXPLICIT,
              status=NodeStatus.ACTIVE, created_by=CreatedByAgent.USER, session_id=test_session_row.id, metadata_={})
    db_session.add_all([n1, n2])
    await db_session.commit()

    from aether.skills.safety.contradiction_enforcer import contradiction_enforcer

    await contradiction_enforcer(n1.id, n2.id, test_session_row.id, db_session)

    await db_session.refresh(n1)
    await db_session.refresh(n2)
    assert n1.status == NodeStatus.FLAGGED
    assert n2.status == NodeStatus.FLAGGED

    link = (await db_session.execute(select(NodeLink).where(NodeLink.source_id == n1.id, NodeLink.target_id == n2.id))).scalar_one()
    assert link.link_type.value == "contradicts"

    from aether.models.runtime import PendingEscalation

    escalations = (await db_session.execute(select(PendingEscalation))).scalars().all()
    assert len(escalations) >= 1


@pytest.mark.asyncio
async def test_inv07_contradiction_not_resolved_automatically(db_session, test_session_row):
    from aether.models.nodes import NodePillar

    n1 = Node(type="fact", title="A", content="a", source=NodeSource.USER_EXPLICIT, confidence=ConfidenceLevel.EXPLICIT,
              status=NodeStatus.FLAGGED, created_by=CreatedByAgent.USER, session_id=test_session_row.id, metadata_={})
    n2 = Node(type="fact", title="B", content="b", source=NodeSource.USER_EXPLICIT, confidence=ConfidenceLevel.EXPLICIT,
              status=NodeStatus.FLAGGED, created_by=CreatedByAgent.USER, session_id=test_session_row.id, metadata_={})
    db_session.add_all([n1, n2])
    await db_session.flush()
    db_session.add(NodePillar(node_id=n1.id, pillar=PillarName.LEGAL, is_primary=True, assigned_by=CreatedByAgent.USER))
    db_session.add(NodePillar(node_id=n2.id, pillar=PillarName.LEGAL, is_primary=True, assigned_by=CreatedByAgent.USER))
    db_session.add(NodeLink(source_id=n1.id, target_id=n2.id, link_type="contradicts", created_by=CreatedByAgent.MASTER_AGENT))
    await db_session.commit()

    from aether.skills.operational.context_assembler import assemble_context

    intent = {"raw_input": "x", "action_type": "query", "subject": "x", "implied_pillars": ["legal"],
              "urgency": "standard", "entities": [], "ambiguity_flag": False}
    await assemble_context({"intent": intent, "session_id": test_session_row.id}, db_session)

    await db_session.refresh(n1)
    await db_session.refresh(n2)
    assert n1.status == NodeStatus.FLAGGED
    assert n2.status == NodeStatus.FLAGGED
    link = (await db_session.execute(select(NodeLink).where(NodeLink.source_id == n1.id, NodeLink.target_id == n2.id))).scalar_one_or_none()
    assert link is not None


# ---------------------------------------------------------------------------
# INV-08
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inv08_watchdog_forces_termination_at_max_iterations(db_session, test_session_row):
    loop_run = LoopRun(loop_type=LoopType.GOAL, trigger="test", session_id=test_session_row.id,
                        status=LoopStatus.RUNNING, iteration_count=1, max_iterations=1, max_duration_ms=999999999)
    db_session.add(loop_run)
    await db_session.commit()

    await LoopWatchdog._check_loops(db_session)

    await db_session.refresh(loop_run)
    assert loop_run.status == LoopStatus.FORCED_TERMINATION

    from aether.models.runtime import PendingEscalation

    esc = (await db_session.execute(select(PendingEscalation).where(PendingEscalation.priority_class == "p0"))).scalars().all()
    assert len(esc) >= 1


@pytest.mark.asyncio
async def test_inv08_loop_start_blocked_when_watchdog_not_running(db_session, test_session_row):
    LoopWatchdog._running = False
    LoopWatchdog._task = None

    with pytest.raises(WatchdogNotRunningError):
        await GoalLoop().start(test_session_row.id, db_session)

    loop_runs = (await db_session.execute(select(LoopRun))).scalars().all()
    assert len(loop_runs) == 0


def test_inv08_assert_loop_within_bounds_raises():
    with pytest.raises(LoopLimitExceeded):
        assert_loop_within_bounds(10, 10, 0, 120000, "test", "test")
    with pytest.raises(LoopLimitExceeded):
        assert_loop_within_bounds(0, 10, 120001, 120000, "test", "test")


# ---------------------------------------------------------------------------
# INV-09
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inv09_skill_log_written_on_success(db_session, test_session_row, monkeypatch):
    async def mock_skill(inputs, db):
        return {"result": "ok"}

    monkeypatch.setitem(__import__("aether.skills.registry", fromlist=["SKILL_REGISTRY"]).SKILL_REGISTRY, "test.mock_skill", mock_skill)
    monkeypatch.setitem(__import__("aether.skills.registry", fromlist=["SKILL_TIMEOUTS"]).SKILL_TIMEOUTS, "test.mock_skill", 5000)

    from aether.skills.invoker import invoke_skill
    from aether.models.runtime import SkillInvocationLog

    result = await invoke_skill("test.mock_skill", {}, test_session_row.id, "master", None, db_session)
    assert result.status == "ok"

    log = (await db_session.execute(select(SkillInvocationLog).where(SkillInvocationLog.id == result.log_id))).scalar_one()
    assert log.status == "ok"
    assert log.inputs_hash is not None
    assert log.outputs_hash is not None
    assert log.latency_ms is not None


@pytest.mark.asyncio
async def test_inv09_skill_log_written_on_timeout(db_session, test_session_row, monkeypatch):
    async def slow_skill(inputs, db):
        await asyncio.sleep(10)

    monkeypatch.setitem(__import__("aether.skills.registry", fromlist=["SKILL_REGISTRY"]).SKILL_REGISTRY, "test.slow_skill", slow_skill)
    monkeypatch.setitem(__import__("aether.skills.registry", fromlist=["SKILL_TIMEOUTS"]).SKILL_TIMEOUTS, "test.slow_skill", 100)

    from aether.skills.invoker import invoke_skill
    from aether.models.runtime import SkillInvocationLog

    with pytest.raises(SkillTimeoutError):
        await invoke_skill("test.slow_skill", {}, test_session_row.id, "master", None, db_session)

    log = (await db_session.execute(select(SkillInvocationLog).where(SkillInvocationLog.skill_name == "test.slow_skill"))).scalar_one()
    assert log.status == "timeout"


@pytest.mark.asyncio
async def test_inv09_skill_log_written_on_error(db_session, test_session_row, monkeypatch):
    async def failing_skill(inputs, db):
        raise RuntimeError("test error")

    monkeypatch.setitem(__import__("aether.skills.registry", fromlist=["SKILL_REGISTRY"]).SKILL_REGISTRY, "test.failing_skill", failing_skill)
    monkeypatch.setitem(__import__("aether.skills.registry", fromlist=["SKILL_TIMEOUTS"]).SKILL_TIMEOUTS, "test.failing_skill", 5000)

    from aether.skills.invoker import invoke_skill
    from aether.models.runtime import SkillInvocationLog

    with pytest.raises(SkillExecutionError):
        await invoke_skill("test.failing_skill", {}, test_session_row.id, "master", None, db_session)

    log = (await db_session.execute(select(SkillInvocationLog).where(SkillInvocationLog.skill_name == "test.failing_skill"))).scalar_one()
    assert log.status == "error"
    assert log.error_detail == "test error"


# ---------------------------------------------------------------------------
# INV-10
# ---------------------------------------------------------------------------


def test_inv10_no_external_api_imports_in_agents_or_skills():
    forbidden = {"requests", "httpx", "aiohttp", "boto3", "stripe"}
    violations = []
    for directory in ("agents", "skills"):
        for path in (_REPO_ROOT / "aether" / directory).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = {node.module.split(".")[0]}
                else:
                    continue
                if names & forbidden:
                    violations.append((str(path), names & forbidden))
    assert violations == [], f"Forbidden external-client imports found: {violations}"
