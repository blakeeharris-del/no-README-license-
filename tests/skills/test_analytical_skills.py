"""
tests.skills.test_analytical_skills
=====================================

Verifies the six Phase-1 analytical skills (SKILL-08..13) against the
real DB, each through a real ``invoke_skill()`` call (EC-19 bar).
Rule-based skills use real nodes with metadata; LLM skills are mocked
at the shared helper. Safety-critical behaviors (legal escalation,
speculative exclusion, health disclaimer/forbidden-word scrub) are
asserted, not assumed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from aether.models.enums import (
    ConfidenceLevel,
    CreatedByAgent,
    EscalationStatus,
    NodeSource,
    NodeStatus,
    NodeType,
    PillarName,
    PriorityClass,
)
from aether.models.nodes import Node, NodePillar
from aether.models.runtime import PendingEscalation, SkillInvocationLog
from aether.skills.invoker import invoke_skill


async def _mknode(db, session_id, pillar, *, ntype=NodeType.FACT,
                  confidence=ConfidenceLevel.EXPLICIT, status=NodeStatus.ACTIVE,
                  metadata=None, title="n", content="c") -> Node:
    node = Node(
        type=ntype, title=title, content=content,
        source=NodeSource.USER_EXPLICIT if confidence == ConfidenceLevel.EXPLICIT else NodeSource.AGENT_WRITE,
        confidence=confidence, status=status,
        created_by=CreatedByAgent.USER if confidence == ConfidenceLevel.EXPLICIT else CreatedByAgent.MASTER_AGENT,
        session_id=session_id, metadata_=metadata or {},
    )
    db.add(node)
    await db.flush()
    db.add(NodePillar(node_id=node.id, pillar=pillar, is_primary=True, assigned_by=node.created_by))
    await db.flush()
    return node


async def _log_ok(db, name, log_id):
    log = (await db.execute(select(SkillInvocationLog).where(SkillInvocationLog.id == log_id))).scalar_one()
    assert log.skill_name == name and log.status == "ok"


# ---- legal_deadline_surfacer (SKILL-08) -------------------------------

@pytest.mark.asyncio
async def test_legal_deadline_surfacer_sorts_scores_escalates(db_session, test_session_row):
    now = datetime.now(timezone.utc)
    await _mknode(db_session, test_session_row.id, PillarName.LEGAL,
                  metadata={"deadline": (now + timedelta(days=10)).isoformat(),
                            "obligation_type": "filing"}, title="File brief")
    await _mknode(db_session, test_session_row.id, PillarName.LEGAL,
                  metadata={"deadline": (now - timedelta(days=3)).isoformat()}, title="Overdue tax")

    result = await invoke_skill(
        "analytical.legal_deadline_surfacer",
        {"days_ahead": 90, "session_id": str(test_session_row.id)},
        test_session_row.id, "legal", None, db_session,
    )
    out = result.output
    assert out["total_found"] == 2
    assert out["overdue_count"] == 1
    # sorted ascending by days_until -> overdue (negative) first
    assert out["deadlines"][0]["days_until"] < 0
    assert out["deadlines"][0]["priority_class"] == "p0"   # overdue always P0
    await _log_ok(db_session, "analytical.legal_deadline_surfacer", result.log_id)

    # The overdue P0 must have produced a pending escalation.
    escs = (await db_session.execute(
        select(PendingEscalation).where(
            PendingEscalation.session_id == test_session_row.id,
            PendingEscalation.status == EscalationStatus.PENDING,
        )
    )).scalars().all()
    assert any(e.priority_class == PriorityClass.P0 for e in escs)


@pytest.mark.asyncio
async def test_legal_deadline_surfacer_empty(db_session, test_session_row):
    result = await invoke_skill(
        "analytical.legal_deadline_surfacer",
        {"days_ahead": 30, "session_id": str(test_session_row.id)},
        test_session_row.id, "legal", None, db_session,
    )
    assert result.output == {"deadlines": [], "total_found": 0, "overdue_count": 0}


# ---- financial_net_worth (SKILL-09) -----------------------------------

@pytest.mark.asyncio
async def test_financial_net_worth_excludes_speculative(db_session, test_session_row):
    await _mknode(db_session, test_session_row.id, PillarName.PERSONAL_FINANCE,
                  metadata={"category": "asset", "amount": "100000"}, title="House")
    await _mknode(db_session, test_session_row.id, PillarName.PERSONAL_FINANCE,
                  metadata={"category": "liability", "amount": "30000"}, title="Loan")
    # speculative asset must be excluded from the total and noted
    await _mknode(db_session, test_session_row.id, PillarName.PERSONAL_FINANCE,
                  confidence=ConfidenceLevel.SPECULATIVE,
                  metadata={"category": "asset", "amount": "999999"}, title="Rumored bonus")

    result = await invoke_skill(
        "analytical.financial_net_worth",
        {"session_id": str(test_session_row.id), "as_of_date": None},
        test_session_row.id, "finance", None, db_session,
    )
    out = result.output
    assert out["total_assets"] == 100000.0
    assert out["total_liabilities"] == 30000.0
    assert out["net_worth"] == 70000.0
    assert out["node_count_used"] == 2               # speculative excluded
    assert "speculative" in (out["missing_data_note"] or "")
    await _log_ok(db_session, "analytical.financial_net_worth", result.log_id)


@pytest.mark.asyncio
async def test_financial_net_worth_empty(db_session, test_session_row):
    result = await invoke_skill(
        "analytical.financial_net_worth",
        {"session_id": str(test_session_row.id)},
        test_session_row.id, "finance", None, db_session,
    )
    assert result.output["net_worth"] == 0
    assert result.output["missing_data_note"] is not None


# ---- relationship_graph (SKILL-13) ------------------------------------

@pytest.mark.asyncio
async def test_relationship_graph_people_and_commitments(db_session, test_session_row):
    await _mknode(db_session, test_session_row.id, PillarName.RELATIONSHIPS,
                  metadata={"entity_type": "person", "name": "Sam",
                            "relationship_tier": "close",
                            "last_contact_date": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()},
                  title="Sam")
    await _mknode(db_session, test_session_row.id, PillarName.RELATIONSHIPS, ntype=NodeType.TASK,
                  metadata={"related_person": "Sam"}, title="Send Sam the docs")

    result = await invoke_skill(
        "analytical.relationship_graph",
        {"session_id": str(test_session_row.id), "tier": None},
        test_session_row.id, "relationships", None, db_session,
    )
    out = result.output
    assert out["total_people"] == 1
    person = out["people"][0]
    assert person["name"] == "Sam"
    assert person["open_commitments"] == 1
    assert out["contacts_due_today"] == 1            # 40 days > close cadence (14)
    await _log_ok(db_session, "analytical.relationship_graph", result.log_id)


# ---- career_trajectory (SKILL-10, LLM) --------------------------------

@pytest.mark.asyncio
async def test_career_trajectory_always_inferred(db_session, test_session_row, mock_llm_client):
    await _mknode(db_session, test_session_row.id, PillarName.CAREER, title="Senior Engineer role")
    mock_llm_client.set_responses([json.dumps({
        "trajectory_summary": "Ascending toward staff level.",
        "current_role": "Senior Engineer", "trajectory_direction": "ascending",
        "credential_gaps": ["system design at scale"],
    })])
    result = await invoke_skill(
        "analytical.career_trajectory",
        {"session_id": str(test_session_row.id), "include_speculative": False},
        test_session_row.id, "career", None, db_session,
    )
    out = result.output
    assert out["confidence"] == "inferred"           # never explicit
    assert out["trajectory_direction"] == "ascending"
    assert len(out["evidence_node_ids"]) == 1         # evidence cited
    await _log_ok(db_session, "analytical.career_trajectory", result.log_id)


# ---- business_health (SKILL-11) ---------------------------------------

@pytest.mark.asyncio
async def test_business_health_at_risk_rule(db_session, test_session_row, mock_llm_client):
    now = datetime.now(timezone.utc)
    for i in range(3):  # >2 overdue obligations -> at_risk (rule, not LLM)
        await _mknode(db_session, test_session_row.id, PillarName.BUSINESS, ntype=NodeType.TASK,
                      metadata={"deadline": (now - timedelta(days=5)).isoformat()},
                      title=f"Overdue {i}")
    mock_llm_client.set_responses([json.dumps({
        "revenue_status": "flat", "strategic_gaps": ["no pipeline"], "health_judgment": "strong",
    })])
    result = await invoke_skill(
        "analytical.business_health",
        {"session_id": str(test_session_row.id), "business_name": None},
        test_session_row.id, "business", None, db_session,
    )
    assert result.output["overall_health"] == "at_risk"   # rule overrides LLM's "strong"
    assert result.output["overdue_obligations"] == 3
    await _log_ok(db_session, "analytical.business_health", result.log_id)


# ---- health_pattern_detector (SKILL-12) -------------------------------

@pytest.mark.asyncio
async def test_health_pattern_detector_safety(db_session, test_session_row, mock_llm_client):
    await _mknode(db_session, test_session_row.id, PillarName.HEALTH,
                  metadata={"category": "lab"}, title="A1C reading")
    await _mknode(db_session, test_session_row.id, PillarName.HEALTH,
                  metadata={"category": "mental_health"}, title="therapy note", content="sensitive")
    # LLM tries to sneak a forbidden clinical word in — must be scrubbed.
    mock_llm_client.set_responses([json.dumps({
        "patterns": [{"pattern_type": "trend", "description": "Consider treatment options.",
                      "evidence_nodes": [], "confidence": "explicit", "date_range": "x to y"}],
    })])
    result = await invoke_skill(
        "analytical.health_pattern_detector",
        {"session_id": str(test_session_row.id), "lookback_days": 180},
        test_session_row.id, "health", None, db_session,
    )
    out = result.output
    assert out["disclaimer"] == "This is a pattern observation, not medical advice or diagnosis."
    # forbidden word scrubbed
    assert all("treatment" not in p["description"] for p in out["patterns"])
    # confidence never explicit
    assert all(p["confidence"] in ("inferred", "speculative") for p in out["patterns"])
    # mental-health acknowledged as present, content withheld
    assert any("Mental health tracking nodes present" in p["description"] for p in out["patterns"])
    await _log_ok(db_session, "analytical.health_pattern_detector", result.log_id)
