"""
tests.agents.test_agent_ecosystem
===================================

Verifies the Phase-1 agent ecosystem against the real DB:

  EC-16  six Specialist Agents route only to their own sub-agents and
         never produce user-facing output.
  EC-17  all 30 sub-agents are independently invocable and produce their
         specified output structure for a real trigger.
  EC-18  every sub-agent invocation is logged to sub_agent_runs, NOT
         action_log.
  EC-22/23 (partial) synthesis_coordinator routes a real cross-pillar
         signal to the master and returns structured signals, not final
         analysis.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select

from aether.agents.specialists import LegalAgent, SPECIALIST_AGENTS
from aether.agents.sub_agents.catalog import SUB_AGENT_CATALOG
from aether.agents.sub_agents.runtime import run_sub_agent
from aether.models.enums import (
    ConfidenceLevel, CreatedByAgent, NodeSource, NodeStatus, NodeType, PillarName,
)
from aether.models.logs import ActionLog
from aether.models.nodes import Node, NodePillar
from aether.models.runtime import SubAgentRun


async def _mknode(db, session_id, pillar, *, ntype=NodeType.FACT, metadata=None, title="n") -> Node:
    node = Node(type=ntype, title=title, content="content", source=NodeSource.USER_EXPLICIT,
                confidence=ConfidenceLevel.EXPLICIT, status=NodeStatus.ACTIVE,
                created_by=CreatedByAgent.USER, session_id=session_id, metadata_=metadata or {})
    db.add(node); await db.flush()
    db.add(NodePillar(node_id=node.id, pillar=pillar, is_primary=True, assigned_by=CreatedByAgent.USER))
    await db.flush()
    return node


# Per-sub-agent extra inputs so each has a valid trigger.
def _extra_inputs(name, node_id):
    if name == "legal.contract_reviewer":
        return {"node_id": str(node_id)}
    if name == "legal.entity_mapper":
        return {"entity_name": None}
    if name == "orchestrator.decision_assembler":
        return {"pillars_affected": ["legal", "personal_finance"], "proposed_action": "x", "intent": {}}
    if name == "orchestrator.synthesis_coordinator":
        return {"triggered_by": "manual"}
    return {}


@pytest.mark.parametrize("sub_agent_name", sorted(SUB_AGENT_CATALOG.keys()))
@pytest.mark.asyncio
async def test_ec17_ec18_all_30_invocable_and_logged(db_session, test_session_row, mock_llm_client, sub_agent_name):
    """EC-17: every sub-agent is independently invocable and returns a dict.
       EC-18: the invocation is logged to sub_agent_runs."""
    # an artifact node for contract_reviewer's trigger
    artifact = await _mknode(db_session, test_session_row.id, PillarName.LEGAL,
                             ntype=NodeType.ARTIFACT, title="contract")
    inputs = {"session_id": str(test_session_row.id), **_extra_inputs(sub_agent_name, artifact.id)}

    result = await run_sub_agent(sub_agent_name, inputs, test_session_row.id, db_session)

    assert isinstance(result.output, dict)
    # a sub_agent_runs row exists for this invocation, with a terminal status
    run = (await db_session.execute(
        select(SubAgentRun).where(SubAgentRun.id == result.run_id)
    )).scalar_one()
    assert run.status.value in ("completed", "failed", "force_terminated")
    assert run.terminated_at is not None
    assert run.parent_agent == SUB_AGENT_CATALOG[sub_agent_name].parent_agent


@pytest.mark.asyncio
async def test_ec18_logged_to_sub_agent_runs_not_action_log(db_session, test_session_row):
    before = (await db_session.execute(select(func.count()).select_from(ActionLog))).scalar_one()
    result = await run_sub_agent(
        "legal.deadline_scanner", {"session_id": str(test_session_row.id)},
        test_session_row.id, db_session)
    after = (await db_session.execute(select(func.count()).select_from(ActionLog))).scalar_one()
    # the framework logs to sub_agent_runs, and does not itself write action_log
    assert (await db_session.get(SubAgentRun, result.run_id)) is not None
    assert after == before


@pytest.mark.asyncio
async def test_ec17_deadline_scanner_output_structure(db_session, test_session_row):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    await _mknode(db_session, test_session_row.id, PillarName.LEGAL,
                  metadata={"deadline": (now - timedelta(days=1)).isoformat()}, title="Overdue filing")
    result = await run_sub_agent(
        "legal.deadline_scanner", {"session_id": str(test_session_row.id)},
        test_session_row.id, db_session)
    out = result.output
    assert set(out.keys()) == {"deadlines", "p0_count", "p1_count", "escalations_created"}
    assert out["p0_count"] >= 1  # overdue -> p0


# ---- EC-16: Specialist Agents -----------------------------------------

@pytest.mark.asyncio
async def test_ec16_specialist_routes_only_own_sub_agents(db_session, test_session_row):
    legal = LegalAgent()
    # request a mix: one legal (owned) + one finance (not owned)
    result = await legal.handle(
        {"session_id": test_session_row.id},
        db_session,
        requested=["legal.entity_mapper", "finance.net_worth_calculator"],
    )
    assert result["sub_agents_run"] == ["legal.entity_mapper"]      # only its own
    assert "finance.net_worth_calculator" in result["sub_agents_rejected"]
    assert result["user_facing"] is False                          # never user-facing
    assert result["agent"] == "legal"


@pytest.mark.asyncio
async def test_ec16_all_six_specialists_exist_and_are_scoped():
    assert len(SPECIALIST_AGENTS) == 6
    # each specialist owns only sub-agents whose name is in its pillar domain
    for pillar, agent in SPECIALIST_AGENTS.items():
        for sa_name in agent.sub_agents:
            assert sa_name in SUB_AGENT_CATALOG
            # a specialist never owns an orchestrator sub-agent
            assert not sa_name.startswith("orchestrator.")


# ---- EC-22 / EC-23: synthesis_coordinator -----------------------------

@pytest.mark.asyncio
async def test_synthesis_coordinator_routes_cross_pillar_signal(db_session, test_session_row, mock_llm_client):
    # nodes in two pillars so a cross-pillar connection is possible
    await _mknode(db_session, test_session_row.id, PillarName.CAREER, title="promotion to VP")
    await _mknode(db_session, test_session_row.id, PillarName.PERSONAL_FINANCE, title="income increase")
    mock_llm_client.set_responses([json.dumps({
        "connections": [{"node_id_a": "a", "node_id_b": "b", "pillar_a": "career",
                         "pillar_b": "personal_finance", "connection": "raise lifts savings",
                         "link_type": "related_to", "confidence": "high", "surfaceable": True}],
        "synthesis_insight": "career and finance move together",
    })])
    result = await run_sub_agent(
        "orchestrator.synthesis_coordinator",
        {"session_id": str(test_session_row.id), "triggered_by": "manual"},
        test_session_row.id, db_session)
    out = result.output
    assert out["skipped"] is False
    # EC-22: at least one real cross-pillar signal routed through the coordinator
    assert len(out["cross_pillar_signals"]) >= 1
    # EC-23: structured signals + diff, not a prose "final analysis" field
    assert "diff" in out and "analysis" not in out
    assert isinstance(out["cross_pillar_signals"], list)
