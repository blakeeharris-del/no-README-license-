"""tests.memory.test_write_protocol — Phase-0 Prompt Section 23."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from aether.invariants.guards import SynthesisFromError
from aether.models.enums import ConfidenceLevel, CreatedByAgent, NodeSource, NodeStatus, PillarName
from aether.models.logs import ActionLog
from aether.models.nodes import Node, NodeLink
from aether.schemas.nodes import ConflictPair, NodeDraft


@pytest.fixture
def _mock_contradiction_scan_for_orchestrator(monkeypatch):
    """
    For tests that go through write_node_with_contradiction_handling()
    (the skill-layer orchestrator) rather than write_node() directly —
    write_node() itself no longer performs any contradiction scanning
    at all (see write_node()'s docstring re: the EC-15 layering fix),
    so plain write_node() tests need no mock. Returns a setter so
    individual tests can supply their own conflict list.
    """
    async def fake_scan(*args, **kwargs):
        return fake_scan.conflicts

    fake_scan.conflicts = []
    monkeypatch.setattr("aether.skills.operational.node_writer._run_contradiction_scan", fake_scan)
    return fake_scan


def test_write_rejects_missing_required_fields():
    with pytest.raises(ValidationError):
        NodeDraft(
            type="fact", title="", content="c", source=NodeSource.USER_EXPLICIT,
            confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
            pillars=[], primary_pillar=PillarName.LEGAL,
        )


def test_write_rejects_title_over_120_chars():
    with pytest.raises(ValidationError):
        NodeDraft(
            type="fact", title="x" * 121, content="c", source=NodeSource.USER_EXPLICIT,
            confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
            pillars=[PillarName.LEGAL], primary_pillar=PillarName.LEGAL,
        )


@pytest.mark.asyncio
async def test_write_detects_contradiction_and_flags_both_nodes(
    db_session, test_session_row, _mock_contradiction_scan_for_orchestrator
):
    existing = Node(
        type="fact", title="Existing fact", content="c", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
        session_id=test_session_row.id, metadata_={},
    )
    db_session.add(existing)
    await db_session.commit()

    _mock_contradiction_scan_for_orchestrator.conflicts = [
        ConflictPair(node_id=existing.id, existing_title=existing.title, conflict_description="conflict", conflict_severity="direct")
    ]

    from aether.skills.operational.node_writer import write_node_with_contradiction_handling

    draft = NodeDraft(
        type="fact", title="New conflicting fact", content="c", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
        pillars=[PillarName.LEGAL], primary_pillar=PillarName.LEGAL,
    )
    result = await write_node_with_contradiction_handling(draft, test_session_row.id, "master", db_session)
    assert result.status == "written_with_contradiction"
    assert existing.id in result.contradiction_node_ids

    await db_session.refresh(existing)
    new_node = (await db_session.execute(select(Node).where(Node.id == result.node_id))).scalar_one()
    assert existing.status == NodeStatus.FLAGGED
    assert new_node.status == NodeStatus.FLAGGED


@pytest.mark.asyncio
async def test_write_creates_supersedes_link_when_user_corrects_existing_fact(db_session, test_session_row):
    from aether.memory.write_protocol import write_node

    first = NodeDraft(
        type="fact", title="Retainer is five thousand", content="original", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
        pillars=[PillarName.BUSINESS], primary_pillar=PillarName.BUSINESS,
    )
    r1 = await write_node(first, test_session_row.id, "master", db_session)

    second = NodeDraft(
        type="fact", title="Retainer is five thousand", content="corrected", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
        pillars=[PillarName.BUSINESS], primary_pillar=PillarName.BUSINESS,
    )
    r2 = await write_node(second, test_session_row.id, "master", db_session)

    old_node = (await db_session.execute(select(Node).where(Node.id == r1.node_id))).scalar_one()
    assert old_node.status == NodeStatus.SUPERSEDED
    link = (await db_session.execute(select(NodeLink).where(NodeLink.source_id == r2.node_id, NodeLink.target_id == r1.node_id))).scalar_one()
    assert link.link_type.value == "supersedes"


@pytest.mark.asyncio
async def test_write_requires_synthesis_from_for_synthesis_source(db_session, test_session_row):
    from aether.memory.write_protocol import write_node

    draft = NodeDraft(
        type="fact", title="Synthesized fact", content="c", source=NodeSource.SYNTHESIS,
        confidence=ConfidenceLevel.INFERRED, created_by=CreatedByAgent.SYNTHESIS_AGENT,
        pillars=[PillarName.LEGAL], primary_pillar=PillarName.LEGAL, metadata={},
    )
    with pytest.raises(SynthesisFromError):
        await write_node(draft, test_session_row.id, "synthesis", db_session)


@pytest.mark.asyncio
async def test_write_creates_action_log_in_same_transaction(db_session, test_session_row):
    from aether.memory.write_protocol import write_node

    draft = NodeDraft(
        type="fact", title="Logged fact", content="c", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
        pillars=[PillarName.LEGAL], primary_pillar=PillarName.LEGAL,
    )
    result = await write_node(draft, test_session_row.id, "master", db_session)
    log = (await db_session.execute(select(ActionLog).where(ActionLog.node_ids.any(result.node_id)))).scalar_one()
    assert log.action_type.value == "write"


@pytest.mark.asyncio
async def test_write_new_user_no_existing_nodes_succeeds(db_session, test_session_row):
    from aether.memory.write_protocol import write_node

    draft = NodeDraft(
        type="fact", title="First ever node", content="c", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, created_by=CreatedByAgent.USER,
        pillars=[PillarName.LEGAL], primary_pillar=PillarName.LEGAL,
    )
    result = await write_node(draft, test_session_row.id, "master", db_session)
    assert result.status == "written"
