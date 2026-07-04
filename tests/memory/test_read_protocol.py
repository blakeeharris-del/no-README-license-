"""tests.memory.test_read_protocol — Phase-0 Prompt Section 23."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aether.memory.read_protocol import (
    fetch_l3,
    fulltext_search,
    read_by_deadline,
    read_by_pillar,
    scan_contradictions,
)
from aether.models.enums import ConfidenceLevel, CreatedByAgent, NodeSource, NodeStatus, PillarName
from aether.models.nodes import Node, NodeLink, NodePillar


async def _make_node(db_session, session_id, pillar, **overrides):
    defaults = dict(
        type="fact", title="Node title", content="content", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, status=NodeStatus.ACTIVE,
        created_by=CreatedByAgent.USER, session_id=session_id, metadata_={},
    )
    defaults.update(overrides)
    node = Node(**defaults)
    db_session.add(node)
    await db_session.flush()
    db_session.add(NodePillar(node_id=node.id, pillar=pillar, is_primary=True, assigned_by=defaults["created_by"]))
    await db_session.commit()
    return node


@pytest.mark.asyncio
async def test_read_excludes_archived_and_superseded_nodes(db_session, test_session_row):
    active = await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="Active", status=NodeStatus.ACTIVE)
    await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="Archived", status=NodeStatus.ARCHIVED)
    await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="Superseded", status=NodeStatus.SUPERSEDED)

    results = await read_by_pillar([PillarName.LEGAL], db_session)
    titles = {n.title for n in results}
    assert "Active" in titles
    assert "Archived" not in titles
    assert "Superseded" not in titles


@pytest.mark.asyncio
async def test_read_excludes_speculative_pending_review_nodes(db_session, test_session_row):
    await _make_node(
        db_session, test_session_row.id, PillarName.LEGAL, title="Speculative pending",
        confidence=ConfidenceLevel.SPECULATIVE, status=NodeStatus.PENDING_REVIEW,
        source=NodeSource.AGENT_WRITE, created_by=CreatedByAgent.MASTER_AGENT,
    )
    results = await read_by_pillar([PillarName.LEGAL], db_session)
    assert results == []


@pytest.mark.asyncio
async def test_deadline_query_returns_nodes_in_correct_date_range(db_session, test_session_row):
    now = datetime.now(timezone.utc)
    in_range = (now + timedelta(days=10)).isoformat()
    out_of_range = (now + timedelta(days=60)).isoformat()

    await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="In range", metadata_={"deadline": in_range})
    await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="Out of range", metadata_={"deadline": out_of_range})

    results = await read_by_deadline([PillarName.LEGAL], now, now + timedelta(days=30), db_session)
    titles = {r.title for r in results}
    assert "In range" in titles
    assert "Out of range" not in titles


@pytest.mark.asyncio
async def test_fulltext_search_returns_matching_results(db_session, test_session_row):
    await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="Vendor contract renewal", content="details")
    await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="Unrelated topic", content="nothing")

    results = await fulltext_search("vendor contract", PillarName.LEGAL, db_session)
    titles = {r.title for r in results}
    assert "Vendor contract renewal" in titles
    assert "Unrelated topic" not in titles


@pytest.mark.asyncio
async def test_fetch_l3_returns_synthesis_nodes_only(db_session, test_session_row):
    await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="L2 fact", source=NodeSource.USER_EXPLICIT)
    await _make_node(
        db_session, test_session_row.id, PillarName.LEGAL, title="L3 belief",
        source=NodeSource.SYNTHESIS, confidence=ConfidenceLevel.INFERRED,
        created_by=CreatedByAgent.SYNTHESIS_AGENT, metadata_={"synthesis_from": []},
    )
    results = await fetch_l3(PillarName.LEGAL, db_session)
    titles = {r.title for r in results}
    assert "L3 belief" in titles
    assert "L2 fact" not in titles


@pytest.mark.asyncio
async def test_scan_contradictions_returns_contradicts_links(db_session, test_session_row):
    n1 = await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="A")
    n2 = await _make_node(db_session, test_session_row.id, PillarName.LEGAL, title="B")
    db_session.add(NodeLink(source_id=n1.id, target_id=n2.id, link_type="contradicts", created_by=CreatedByAgent.MASTER_AGENT))
    await db_session.commit()

    results = await scan_contradictions(db_session)
    pairs = [(r["node_id_a"], r["node_id_b"]) for r in results]
    assert (str(n1.id), str(n2.id)) in pairs
