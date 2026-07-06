"""
tests.skills.test_operational_executive_skills
================================================

Verifies the Phase-1 operational (SKILL-16,17) and executive
(SKILL-20..24) skills against the real DB via real invoke_skill()
calls. Safety-critical behaviors asserted: node_linker's mandatory
contradiction_enforcer trigger (INV-07), deadline escalation, the
Foundation §10.6 recommendation-deferral in decision_protocol, and the
risk->confirmation mapping in approval_presenter.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from aether.invariants.guards import NodeNotFoundError, SkillExecutionError
from aether.models.enums import (
    ConfidenceLevel, CreatedByAgent, LinkType, NodeSource, NodeStatus, NodeType, PillarName,
)
from aether.models.logs import SynthesisRun
from aether.models.nodes import Node, NodeLink, NodePillar
from aether.models.runtime import PendingEscalation, SkillInvocationLog
from aether.skills.invoker import invoke_skill


async def _mknode(db, session_id, pillar, *, ntype=NodeType.FACT, metadata=None, title="n") -> Node:
    node = Node(type=ntype, title=title, content="c", source=NodeSource.USER_EXPLICIT,
                confidence=ConfidenceLevel.EXPLICIT, status=NodeStatus.ACTIVE,
                created_by=CreatedByAgent.USER, session_id=session_id, metadata_=metadata or {})
    db.add(node); await db.flush()
    db.add(NodePillar(node_id=node.id, pillar=pillar, is_primary=True, assigned_by=CreatedByAgent.USER))
    await db.flush()
    return node


async def _log_ok(db, name, log_id):
    log = (await db.execute(select(SkillInvocationLog).where(SkillInvocationLog.id == log_id))).scalar_one()
    assert log.skill_name == name and log.status == "ok"


# ---- node_linker (SKILL-16) -------------------------------------------

@pytest.mark.asyncio
async def test_node_linker_creates_and_dedups(db_session, test_session_row):
    a = await _mknode(db_session, test_session_row.id, PillarName.LEGAL, title="A")
    b = await _mknode(db_session, test_session_row.id, PillarName.LEGAL, title="B")
    inp = {"source_id": str(a.id), "target_id": str(b.id), "link_type": "related_to",
           "created_by": "master_agent", "notes": None, "session_id": str(test_session_row.id)}
    r1 = await invoke_skill("operational.node_linker", inp, test_session_row.id, "master", None, db_session)
    assert r1.output["status"] == "created"
    r2 = await invoke_skill("operational.node_linker", inp, test_session_row.id, "master", None, db_session)
    assert r2.output["status"] == "duplicate_skipped"   # uniqueness
    await _log_ok(db_session, "operational.node_linker", r1.log_id)


@pytest.mark.asyncio
async def test_node_linker_contradicts_triggers_enforcer(db_session, test_session_row):
    a = await _mknode(db_session, test_session_row.id, PillarName.LEGAL, title="claim A")
    b = await _mknode(db_session, test_session_row.id, PillarName.LEGAL, title="claim B")
    r = await invoke_skill(
        "operational.node_linker",
        {"source_id": str(a.id), "target_id": str(b.id), "link_type": "contradicts",
         "created_by": "master_agent", "session_id": str(test_session_row.id)},
        test_session_row.id, "master", None, db_session,
    )
    assert r.output["triggered_enforcer"] is True     # INV-07 mandatory
    # enforcer flags both nodes + inserts an escalation
    refetched = await db_session.get(Node, a.id)
    assert refetched.status == NodeStatus.FLAGGED
    escs = (await db_session.execute(
        select(PendingEscalation).where(PendingEscalation.session_id == test_session_row.id)
    )).scalars().all()
    assert len(escs) >= 1


@pytest.mark.asyncio
async def test_node_linker_missing_node_raises(db_session, test_session_row):
    a = await _mknode(db_session, test_session_row.id, PillarName.LEGAL, title="A")
    from aether.skills.operational.node_linker import link_nodes
    with pytest.raises(NodeNotFoundError):
        await link_nodes(
            {"source_id": str(a.id), "target_id": str(uuid.uuid4()), "link_type": "related_to",
             "created_by": "master_agent", "session_id": str(test_session_row.id)},
            db_session,
        )


# ---- deadline_monitor (SKILL-17) --------------------------------------

@pytest.mark.asyncio
async def test_deadline_monitor_counts_and_escalates(db_session, test_session_row):
    now = datetime.now(timezone.utc)
    await _mknode(db_session, test_session_row.id, PillarName.BUSINESS,
                  metadata={"deadline": (now - timedelta(days=2)).isoformat()}, title="Overdue biz")
    r = await invoke_skill(
        "operational.deadline_monitor",
        {"session_id": str(test_session_row.id), "pillars": None},
        test_session_row.id, "master", None, db_session,
    )
    out = r.output
    assert out["deadlines_found"] >= 1
    assert out["p0_count"] >= 1                        # overdue -> p0
    assert out["escalations_created"] >= 1
    # re-run: dedup means no new escalations created
    r2 = await invoke_skill(
        "operational.deadline_monitor",
        {"session_id": str(test_session_row.id), "pillars": None},
        test_session_row.id, "master", None, db_session,
    )
    assert r2.output["escalations_created"] == 0
    await _log_ok(db_session, "operational.deadline_monitor", r.log_id)


# ---- approval_presenter (SKILL-23) ------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("risk,expected", [
    ("low", "yes/no"), ("medium", "type CONFIRM"),
    ("high", "type the action name exactly"), ("restricted", "type CONFIRM plus reason"),
])
async def test_approval_presenter_confirmation_mapping(db_session, test_session_row, risk, expected):
    r = await invoke_skill(
        "executive.approval_presenter",
        {"action": "Transfer", "target": "acct", "amount_or_consequence": "$5000",
         "timing": "now", "authority_level": 3, "risk_level": risk},
        test_session_row.id, "master", None, db_session,
    )
    assert r.output["confirmation_required"] == expected
    assert expected in r.output["approval_text"]


# ---- decision_protocol (SKILL-22, Foundation §10.6) -------------------

@pytest.mark.asyncio
async def test_decision_protocol_defers_recommendation_by_default(db_session, test_session_row, mock_llm_client):
    mock_llm_client.set_responses([json.dumps({
        "sense_summary": "User weighing a job change.",
        "analysis": "Two options with tradeoffs.",
        "challenge": "Compensation data is missing.",
        "recommendation_if_asked": "Option A.",
    })])
    r = await invoke_skill(
        "executive.decision_protocol",
        {"proposed_action": "Consider the offer", "relevant_nodes": [],
         "user_intent": {"action_type": "query", "raw_input": "help me think about this",
                         "subject": "job offer", "implied_pillars": ["career"], "urgency": "standard"},
         "session_id": str(test_session_row.id)},
        test_session_row.id, "master", None, db_session,
    )
    out = r.output
    # Foundation §10.6: no recommendation by default.
    assert "No recommendation is offered by default" in out["recommendation"]
    assert out["challenge"]                             # always surfaces unknowns
    assert out["auto_executable"] is True               # query -> L0
    await _log_ok(db_session, "executive.decision_protocol", r.log_id)


@pytest.mark.asyncio
async def test_decision_protocol_recommends_when_asked_and_flags_external(db_session, test_session_row, mock_llm_client):
    mock_llm_client.set_responses([json.dumps({
        "sense_summary": "s", "analysis": "a", "challenge": "c",
        "recommendation_if_asked": "Do X.",
    })])
    r = await invoke_skill(
        "executive.decision_protocol",
        {"proposed_action": "Send the filing", "relevant_nodes": [],
         "user_intent": {"action_type": "task", "raw_input": "what should i do here, recommend a path",
                         "subject": "filing", "implied_pillars": ["legal"], "urgency": "immediate"},
         "session_id": str(test_session_row.id)},
        test_session_row.id, "master", None, db_session,
    )
    out = r.output
    assert out["recommendation"] == "Do X."             # explicit ask -> recommend
    assert out["approval_required"] is True             # task -> L3 external state
    assert out["approval_request"] is not None


# ---- session_briefer (SKILL-20) ---------------------------------------

@pytest.mark.asyncio
async def test_session_briefer_empty_l1(db_session, test_session_row, mock_llm_client):
    r = await invoke_skill(
        "executive.session_briefer",
        {"session_id": str(test_session_row.id),
         "l1": {"session_id": str(test_session_row.id), "open_tasks": [], "upcoming_deadlines": [],
                "flagged_nodes": [], "pending_reviews": 0, "contradiction_count": 0}},
        test_session_row.id, "master", None, db_session,
    )
    assert "fresh start" in r.output["brief_text"]
    await _log_ok(db_session, "executive.session_briefer", r.log_id)


# ---- weekly_reviewer (SKILL-21) ---------------------------------------

@pytest.mark.asyncio
async def test_weekly_reviewer_structure(db_session, test_session_row, mock_llm_client):
    mock_llm_client.set_responses([json.dumps({
        "review_text": "A steady week.", "focus_recommendations": ["Follow up on legal filing"],
    })])
    r = await invoke_skill(
        "executive.weekly_reviewer",
        {"session_id": str(test_session_row.id),
         "since_date": (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()},
        test_session_row.id, "master", None, db_session,
    )
    out = r.output
    assert set(out["pillar_snapshots"].keys()) == {p.value for p in PillarName}
    assert out["focus_recommendations"] == ["Follow up on legal filing"]
    await _log_ok(db_session, "executive.weekly_reviewer", r.log_id)


# ---- synthesis_diff_presenter (SKILL-24) ------------------------------

@pytest.mark.asyncio
async def test_synthesis_diff_presenter_separates_speculative(db_session, test_session_row):
    run = SynthesisRun(triggered_by="manual", diff_report={
        "new_nodes": [
            {"node_id": str(uuid.uuid4()), "title": "inferred belief", "confidence": "inferred"},
            {"node_id": str(uuid.uuid4()), "title": "spec belief", "confidence": "speculative",
             "content": "needs confirmation"},
        ],
    })
    db_session.add(run); await db_session.flush()
    r = await invoke_skill(
        "executive.synthesis_diff_presenter",
        {"synthesis_run_id": str(run.id), "session_id": str(test_session_row.id)},
        test_session_row.id, "master", None, db_session,
    )
    out = r.output
    assert len(out["new_beliefs"]) == 1                 # inferred auto-displayed
    assert len(out["requires_confirmation"]) == 1       # speculative held for confirmation
    assert out["total_changes"] == 2
    await _log_ok(db_session, "executive.synthesis_diff_presenter", r.log_id)
