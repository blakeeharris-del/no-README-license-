"""
tests.skills.test_cognitive_skills
====================================

Verifies the four Phase-1 cognitive skills (SKILL-03..06), each driven
through a real ``invoke_skill()`` call so it lands a genuine
``skill_invocation_log`` row — EC-19's actual bar ("real invocations,
not stub rows"). confidence_scorer is rule-based; the other three are
mocked at the shared LLM helper.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from aether.models.enums import (
    ConfidenceLevel,
    CreatedByAgent,
    NodeSource,
    NodeStatus,
    NodeType,
    PillarName,
)
from aether.models.nodes import Node, NodePillar
from aether.models.runtime import SkillInvocationLog
from aether.skills.invoker import invoke_skill


async def _make_node(db, session_id, confidence: ConfidenceLevel) -> Node:
    node = Node(
        type=NodeType.FACT,
        title="supporting fact",
        content="content",
        source=NodeSource.USER_EXPLICIT if confidence == ConfidenceLevel.EXPLICIT else NodeSource.AGENT_WRITE,
        confidence=confidence,
        status=NodeStatus.ACTIVE,
        created_by=CreatedByAgent.USER if confidence == ConfidenceLevel.EXPLICIT else CreatedByAgent.MASTER_AGENT,
        session_id=session_id,
        metadata_={},
    )
    db.add(node)
    await db.flush()
    db.add(NodePillar(node_id=node.id, pillar=PillarName.PERSONAL_FINANCE, is_primary=True, assigned_by=node.created_by))
    await db.flush()
    return node


async def _assert_logged(db, name: str, log_id) -> None:
    log = (
        await db.execute(select(SkillInvocationLog).where(SkillInvocationLog.id == log_id))
    ).scalar_one()
    assert log.skill_name == name
    assert log.status == "ok"
    assert log.latency_ms is not None


# ---- confidence_scorer (rule-based, SKILL-03) -------------------------

@pytest.mark.asyncio
async def test_confidence_scorer_synthesis_inferred(db_session, test_session_row):
    ids = [str((await _make_node(db_session, test_session_row.id, ConfidenceLevel.EXPLICIT)).id)
           for _ in range(3)]
    result = await invoke_skill(
        "cognitive.confidence_scorer",
        {"node_draft": {"title": "net worth", "content": "...", "source": "synthesis"},
         "supporting_node_ids": ids, "pillar": "personal_finance"},
        test_session_row.id, "master", None, db_session,
    )
    assert result.status == "ok"
    assert result.output["confidence"] == "inferred"
    assert result.output["support_count"] == 3
    assert result.output["demotion_risk"] is False
    await _assert_logged(db_session, "cognitive.confidence_scorer", result.log_id)


@pytest.mark.asyncio
async def test_confidence_scorer_speculative_when_thin_and_demotion(db_session, test_session_row):
    spec = await _make_node(db_session, test_session_row.id, ConfidenceLevel.SPECULATIVE)
    result = await invoke_skill(
        "cognitive.confidence_scorer",
        {"node_draft": {"title": "x", "content": "...", "source": "synthesis"},
         "supporting_node_ids": [str(spec.id)], "pillar": "personal_finance"},
        test_session_row.id, "master", None, db_session,
    )
    assert result.output["confidence"] == "speculative"  # <3 supporting
    assert result.output["demotion_risk"] is True         # a supporting node is speculative


@pytest.mark.asyncio
async def test_confidence_scorer_bad_source_is_speculative(db_session, test_session_row):
    result = await invoke_skill(
        "cognitive.confidence_scorer",
        {"node_draft": {"title": "x", "content": "...", "source": "not_a_real_source"},
         "supporting_node_ids": [], "pillar": "legal"},
        test_session_row.id, "master", None, db_session,
    )
    assert result.output["confidence"] == "speculative"  # FM-02


@pytest.mark.asyncio
async def test_confidence_scorer_never_explicit_for_agent_write(db_session, test_session_row):
    result = await invoke_skill(
        "cognitive.confidence_scorer",
        {"node_draft": {"title": "x", "content": "...", "source": "agent_write"},
         "supporting_node_ids": [], "pillar": "legal"},
        test_session_row.id, "master", None, db_session,
    )
    assert result.output["confidence"] == "inferred"  # INV-03: capped, never explicit


# ---- synthesis_engine (LLM, SKILL-04) ---------------------------------

@pytest.mark.asyncio
async def test_synthesis_engine_sanitizes_output(db_session, test_session_row, mock_llm_client):
    mock_llm_client.set_responses([json.dumps({
        "candidates": [
            {"title": "belief A", "content": "c", "confidence": "explicit",   # must be demoted
             "synthesis_from": ["11111111-1111-1111-1111-111111111111"],
             "supersedes_l3_id": None, "candidate_type": "new_belief"},
            {"title": "unsupported", "content": "c", "confidence": "inferred",
             "synthesis_from": [], "candidate_type": "pattern"},              # dropped: empty synthesis_from
        ],
        "contradictions_found": [],
    })])
    result = await invoke_skill(
        "cognitive.synthesis_engine",
        {"pillar": "personal_finance",
         "l2_nodes": [{"id": "11111111-1111-1111-1111-111111111111", "title": "t", "content": "c"}],
         "existing_l3": [], "session_id": str(test_session_row.id)},
        test_session_row.id, "synthesis", None, db_session,
    )
    cands = result.output["candidates"]
    assert len(cands) == 1                       # unsupported candidate dropped
    assert cands[0]["confidence"] == "inferred"  # 'explicit' demoted (INV-03)
    await _assert_logged(db_session, "cognitive.synthesis_engine", result.log_id)


@pytest.mark.asyncio
async def test_synthesis_engine_empty_l2_returns_empty(db_session, test_session_row, mock_llm_client):
    result = await invoke_skill(
        "cognitive.synthesis_engine",
        {"pillar": "legal", "l2_nodes": [], "existing_l3": [], "session_id": str(test_session_row.id)},
        test_session_row.id, "synthesis", None, db_session,
    )
    assert result.output == {"candidates": [], "contradictions_found": []}


# ---- decision_framer (LLM, SKILL-05) ----------------------------------

@pytest.mark.asyncio
async def test_decision_framer_appends_disclaimer(db_session, test_session_row, mock_llm_client):
    mock_llm_client.set_responses([json.dumps({
        "options": [{"label": "A", "description": "d", "pros": ["p"], "cons": ["c"]}],
        "assumptions": ["a"], "risks": ["r"], "recommendation": "Go with A.",
        "confidence": "medium", "missing_info": [],
    })])
    result = await invoke_skill(
        "cognitive.decision_framer",
        {"proposed_action": "Take the job", "relevant_nodes": [],
         "pillars_affected": ["career"], "urgency": "standard"},
        test_session_row.id, "master", None, db_session,
    )
    assert "This is a recommendation, not a decision. You decide." in result.output["recommendation"]
    await _assert_logged(db_session, "cognitive.decision_framer", result.log_id)


# ---- cross_pillar_connector (LLM, SKILL-06) ---------------------------

@pytest.mark.asyncio
async def test_cross_pillar_connector_filters_same_pillar(db_session, test_session_row, mock_llm_client):
    mock_llm_client.set_responses([json.dumps({
        "connections": [
            {"node_id_a": "a", "node_id_b": "b", "pillar_a": "career", "pillar_b": "personal_finance",
             "connection": "raise affects savings", "link_type": "related_to",
             "confidence": "high", "surfaceable": True},
            {"node_id_a": "c", "node_id_b": "d", "pillar_a": "legal", "pillar_b": "legal",  # same pillar -> dropped
             "connection": "x", "link_type": "related_to", "confidence": "low", "surfaceable": False},
        ],
        "synthesis_insight": "career and finance are linked",
    })])
    result = await invoke_skill(
        "cognitive.cross_pillar_connector",
        {"nodes": [{"id": "a", "title": "raise", "pillar": "career"},
                   {"id": "b", "title": "savings", "pillar": "personal_finance"}],
         "primary_intent": "career move", "session_id": str(test_session_row.id)},
        test_session_row.id, "master", None, db_session,
    )
    conns = result.output["connections"]
    assert len(conns) == 1                       # same-pillar connection filtered out
    assert conns[0]["pillar_a"] != conns[0]["pillar_b"]
    assert result.output["synthesis_insight"] is not None
    await _assert_logged(db_session, "cognitive.cross_pillar_connector", result.log_id)
