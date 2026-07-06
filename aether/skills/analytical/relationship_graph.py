"""
aether.skills.analytical.relationship_graph
==============================================

SKILL-13 (Missing Specs). Builds a structured view of key people, open
commitments, and last-contact dates from the relationships pillar.
Rule-based (no LLM); L0 read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aether.memory.read_protocol import read_pillar_nodes
from aether.models.enums import NodeStatus, NodeType, PillarName

logger = logging.getLogger("aether.skills.analytical.relationship_graph")

# Default contact cadence (days) by tier, used for contacts_due_today.
_CADENCE = {"close": 14, "professional": 30, "extended": 90}


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).days
    except (ValueError, TypeError):
        return None


async def build_relationship_graph(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "tier": str|null}``.
    Returns the relationship snapshot (see SKILL-13 outputs).
    """
    tier_filter = inputs.get("tier")
    now = datetime.now(timezone.utc)

    nodes = await read_pillar_nodes(
        [PillarName.RELATIONSHIPS], db, node_types=[NodeType.FACT, NodeType.TASK]
    )

    # People: fact nodes tagged as a person.
    people_nodes = [
        n for n in nodes
        if n.type == NodeType.FACT and (n.metadata_ or {}).get("entity_type") == "person"
    ]
    # Commitments: task nodes referencing a person.
    commitment_nodes = [n for n in nodes if n.type == NodeType.TASK]

    # Index open commitments and overdue commitments by related person.
    commitments_by_person: dict[str, list[str]] = {}
    overdue_commitments = 0
    for c in commitment_nodes:
        meta = c.metadata_ or {}
        person = meta.get("related_person")
        if person and c.status == NodeStatus.ACTIVE:
            commitments_by_person.setdefault(person, []).append(str(c.id))
            deadline = meta.get("deadline")
            ds = _days_since(deadline, now)
            if ds is not None and ds > 0:  # deadline already in the past
                overdue_commitments += 1

    people = []
    contacts_due_today = 0
    for node in people_nodes:
        meta = node.metadata_ or {}
        tier = meta.get("relationship_tier", "extended")
        if tier_filter and tier != tier_filter:
            continue
        name = meta.get("name") or node.title
        last_contact = meta.get("last_contact_date")
        days_since = _days_since(last_contact, now)
        commitment_ids = commitments_by_person.get(name, [])

        cadence = _CADENCE.get(tier)
        if days_since is not None and cadence is not None and days_since >= cadence:
            contacts_due_today += 1

        people.append({
            "node_id": str(node.id),
            "name": name,
            "tier": tier,
            "last_contact": last_contact,
            "days_since": days_since,
            "open_commitments": len(commitment_ids),
            "commitment_node_ids": commitment_ids,
        })

    return {
        "people": people,
        "total_people": len(people),
        "overdue_commitments": overdue_commitments,
        "contacts_due_today": contacts_due_today,
    }
