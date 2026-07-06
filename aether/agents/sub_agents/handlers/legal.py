"""Legal pillar sub-agent handlers (SA-01, SA-02, SA-03, SA-04, SA-22)."""

from __future__ import annotations

import json

from aether.agents.sub_agents.handlers._common import call_skill, days_until, nodes_for, priority_for_days
from aether.models.enums import NodeSource, PillarName
from aether.models.nodes import Node
from aether.skills._llm import call_json
from sqlalchemy import select


async def deadline_scanner(inputs: dict, db) -> dict:
    """SA-01: legal deadlines in next 90 days, scored + escalated."""
    out = await call_skill("analytical.legal_deadline_surfacer",
                           {"days_ahead": inputs.get("days_ahead", 90),
                            "session_id": inputs["session_id"]},
                           db, inputs["session_id"], "legal.deadline_scanner")
    deadlines = out.get("deadlines", [])
    p0 = [d for d in deadlines if d["priority_class"] == "p0"]
    p1 = [d for d in deadlines if d["priority_class"] == "p1"]
    return {"deadlines": deadlines, "p0_count": len(p0), "p1_count": len(p1),
            "escalations_created": len(p0) + len(p1)}


async def contract_reviewer(inputs: dict, db) -> dict:
    """SA-02: parse a contract artifact node into structured metadata."""
    node_id = inputs["node_id"]
    node = (await db.execute(select(Node).where(Node.id == node_id))).scalar_one_or_none()
    if node is None:
        from aether.invariants.guards import NodeNotFoundError
        raise NodeNotFoundError(node_id)

    system = (
        "Extract contract metadata. Return ONLY JSON: "
        '{"parties":[str],"effective_date":str|null,"expiry_date":str|null,'
        '"obligations":[{"party":str,"obligation":str,"deadline":str|null}],'
        '"rights":[str],"jurisdiction":str|null}'
    )
    parsed = await call_json(system, (node.content or "")[:10000],
                             logger=__import__("logging").getLogger("legal.contract_reviewer"),
                             max_tokens=1024) or {}
    return {
        "parties": parsed.get("parties", []),
        "effective_date": parsed.get("effective_date"),
        "expiry_date": parsed.get("expiry_date"),
        "obligations": parsed.get("obligations", []),
        "rights": parsed.get("rights", []),
        "jurisdiction": parsed.get("jurisdiction"),
        "write_proposals": [],  # returned to parent for confirmation before writing
    }


async def entity_mapper(inputs: dict, db) -> dict:
    """SA-03: build entity map from legal entity nodes."""
    entity_name = inputs.get("entity_name")
    nodes = await nodes_for(PillarName.LEGAL, db)
    entities = []
    for n in nodes:
        meta = n.metadata_ or {}
        if meta.get("entity_type") not in ("LLC", "Corp", "Partnership", "Trust", "legal_entity"):
            continue
        if entity_name and (meta.get("name") or n.title) != entity_name:
            continue
        entities.append({
            "node_id": str(n.id), "name": meta.get("name") or n.title,
            "entity_type": meta.get("entity_type"), "jurisdiction": meta.get("jurisdiction"),
            "owners": meta.get("owners", []), "officers": meta.get("officers", []),
            "linked_contracts": meta.get("linked_contracts", []),
        })
    return {"entities": entities}


async def obligation_tracker(inputs: dict, db) -> dict:
    """SA-04: legal obligations approaching deadline, P-classified."""
    out = await call_skill("analytical.legal_deadline_surfacer",
                           {"days_ahead": inputs.get("days_ahead", 30),
                            "session_id": inputs["session_id"]},
                           db, inputs["session_id"], "legal.obligation_tracker")
    return {"obligations": out.get("deadlines", []),
            "total": out.get("total_found", 0)}


async def regulatory_compliance_scanner(inputs: dict, db) -> dict:
    """SA-22: regulatory compliance items with approaching deadlines."""
    nodes = await nodes_for(PillarName.LEGAL, db)
    items = []
    for n in nodes:
        meta = n.metadata_ or {}
        if meta.get("type") != "regulatory" and meta.get("category") != "regulatory":
            continue
        du = days_until(meta.get("deadline"))
        items.append({"node_id": str(n.id), "title": n.title,
                      "jurisdiction": meta.get("jurisdiction"), "deadline": meta.get("deadline"),
                      "days_until": du, "priority_class": priority_for_days(du, p1_within=30)})
    items.sort(key=lambda x: (x["days_until"] is None, x["days_until"]))
    return {"compliance_items": items, "total": len(items)}
