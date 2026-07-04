"""tests.agents.test_context_packet — Phase-0 Prompt Section 23."""

from __future__ import annotations

import pytest

from aether.invariants.guards import ContextPacketValidationError
from aether.models.enums import ConfidenceLevel, CreatedByAgent, NodeSource, NodeStatus, PillarName
from aether.models.nodes import Node, NodePillar
from aether.schemas.agent import ContextPacket
from aether.skills.operational.context_assembler import assemble_context

_INTENT = {
    "raw_input": "what's my legal exposure", "action_type": "query", "subject": "legal exposure",
    "implied_pillars": ["legal"], "urgency": "standard", "entities": [], "ambiguity_flag": False,
}


@pytest.mark.asyncio
async def test_packet_missing_section_raises_validation_error():
    incomplete = {"user_intent": {}, "active_pillar": {"pillar": "legal", "routing_mode": "direct"}}
    with pytest.raises(Exception):
        try:
            ContextPacket.model_validate(incomplete)
        except Exception as exc:
            raise ContextPacketValidationError(str(exc)) from exc


@pytest.mark.asyncio
async def test_packet_excludes_speculative_pending_review_nodes(db_session, test_session_row):
    node = Node(
        type="fact", title="Speculative pending", content="c", source=NodeSource.AGENT_WRITE,
        confidence=ConfidenceLevel.SPECULATIVE, status=NodeStatus.PENDING_REVIEW,
        created_by=CreatedByAgent.MASTER_AGENT, session_id=test_session_row.id, metadata_={},
    )
    db_session.add(node)
    await db_session.flush()
    db_session.add(NodePillar(node_id=node.id, pillar=PillarName.LEGAL, is_primary=True, assigned_by=CreatedByAgent.MASTER_AGENT))
    await db_session.commit()

    packet = await assemble_context({"intent": _INTENT, "session_id": test_session_row.id}, db_session)
    node_ids = [n["id"] for n in packet["relevant_nodes"]["nodes"]]
    assert str(node.id) not in node_ids


@pytest.mark.asyncio
async def test_packet_all_six_sections_present_and_correctly_typed(db_session, test_session_row):
    packet = await assemble_context({"intent": _INTENT, "session_id": test_session_row.id}, db_session)
    validated = ContextPacket.model_validate(packet)  # raises if any section missing/mistyped
    assert validated.active_pillar.pillar == "legal"
    assert set(packet.keys()) == {
        "user_intent", "active_pillar", "relevant_nodes", "session_state", "instructions", "output_format"
    }


@pytest.mark.asyncio
async def test_packet_token_budget_enforced_and_truncated_flag_set(db_session, test_session_row, monkeypatch):
    from aether.config import settings

    monkeypatch.setattr(settings, "llm_context_token_budget", 60)  # tiny budget forces truncation

    for i in range(5):
        node = Node(
            type="fact", title=f"Node {i}", content="x" * 400, source=NodeSource.USER_EXPLICIT,
            confidence=ConfidenceLevel.EXPLICIT, status=NodeStatus.ACTIVE,
            created_by=CreatedByAgent.USER, session_id=test_session_row.id, metadata_={},
        )
        db_session.add(node)
        await db_session.flush()
        db_session.add(NodePillar(node_id=node.id, pillar=PillarName.LEGAL, is_primary=True, assigned_by=CreatedByAgent.USER))
    await db_session.commit()

    packet = await assemble_context({"intent": _INTENT, "session_id": test_session_row.id}, db_session)
    assert packet["relevant_nodes"]["truncated"] is True
    assert len(packet["relevant_nodes"]["nodes"]) < 5


@pytest.mark.asyncio
async def test_packet_hard_constraints_block_present_in_instructions(db_session, test_session_row):
    packet = await assemble_context({"intent": _INTENT, "session_id": test_session_row.id}, db_session)
    constraints = packet["instructions"]["hard_constraints"]
    assert len(constraints) == 7
    assert any("source node ID" in c for c in constraints)
