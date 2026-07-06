"""
aether.skills.operational.node_linker
========================================

SKILL-16 (Missing Specs). Creates a typed, directed link between two
existing nodes. Enforces the (source, target, link_type) uniqueness
constraint and, when ``link_type == "contradicts"``, mandatorily
triggers safety.contradiction_enforcer (INV-07).

Discrepancy A note: the catalog tags this Phase-0, but Phase-0 never
built it (only a forward-reference comment existed). Resolved per
governance precedence to Phase-1; the "Phase 0" tag is stale.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from aether.invariants.guards import NodeNotFoundError, NodeValidationError
from aether.models.enums import CreatedByAgent, LinkType
from aether.models.nodes import Node, NodeLink
from aether.skills.safety.contradiction_enforcer import contradiction_enforcer

logger = logging.getLogger("aether.skills.operational.node_linker")


async def link_nodes(inputs: dict, db) -> dict:
    """
    inputs: ``{"source_id", "target_id", "link_type", "created_by",
               "notes": str|null, "session_id"}``.
    Returns ``{"link_id", "status", "triggered_enforcer"}``.
    """
    # FM-02: invalid link_type / created_by -> ValidationError.
    try:
        source_id = uuid.UUID(str(inputs["source_id"]))
        target_id = uuid.UUID(str(inputs["target_id"]))
        link_type = LinkType(inputs["link_type"])
        created_by = CreatedByAgent(inputs.get("created_by", CreatedByAgent.MASTER_AGENT.value))
    except (ValueError, KeyError, TypeError) as exc:
        raise NodeValidationError(f"node_linker invalid input: {exc}") from exc

    # FM-01: both endpoints must exist.
    found = set(
        (await db.execute(select(Node.id).where(Node.id.in_([source_id, target_id])))).scalars().all()
    )
    for nid in (source_id, target_id):
        if nid not in found:
            raise NodeNotFoundError(nid)

    # Uniqueness: (source, target, link_type). Duplicate -> skip, do not raise.
    existing = (
        await db.execute(
            select(NodeLink.id).where(
                NodeLink.source_id == source_id,
                NodeLink.target_id == target_id,
                NodeLink.link_type == link_type,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"link_id": str(existing), "status": "duplicate_skipped", "triggered_enforcer": False}

    link = NodeLink(
        source_id=source_id,
        target_id=target_id,
        link_type=link_type,
        created_by=created_by,
        notes=inputs.get("notes"),
    )
    db.add(link)
    await db.flush()  # FM-03: surface any write failure before enforcer runs

    # INV-07: contradicts links MUST trigger the enforcer. Mandatory.
    triggered = False
    if link_type == LinkType.CONTRADICTS:
        session_id = uuid.UUID(str(inputs["session_id"]))
        await contradiction_enforcer(source_id, target_id, session_id, db)
        triggered = True

    return {"link_id": str(link.id), "status": "created", "triggered_enforcer": triggered}
