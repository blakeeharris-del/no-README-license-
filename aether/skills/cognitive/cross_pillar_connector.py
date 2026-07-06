"""
aether.skills.cognitive.cross_pillar_connector
=================================================

SKILL-06 (Missing Specs). Given a node set merged from 2+ pillars,
identify meaningful connections that cross pillar boundaries and return
link recommendations for the master agent to surface or confirm.

This skill does NOT write node_links and does NOT write nodes — it only
recommends. It is the source of the cross-pillar signal EC-22 checks
for; those recommendations are routed through
orchestrator.synthesis_coordinator to the master agent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aether.models.enums import LinkType
from aether.skills._llm import call_json

logger = logging.getLogger("aether.skills.cognitive.cross_pillar_connector")

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "agents" / "prompts"
_VALID_LINK_TYPES = {lt.value for lt in LinkType}
_EMPTY = {"connections": [], "synthesis_insight": None}


def _system_prompt() -> str:
    base = (_PROMPTS_DIR / "base_system.txt").read_text()
    framing = (
        "\n\nTASK: Identify semantic connections BETWEEN nodes that belong to "
        "DIFFERENT pillars. Ignore within-pillar links. Return ONLY JSON:\n"
        '{"connections":[{"node_id_a":str,"node_id_b":str,"pillar_a":str,'
        '"pillar_b":str,"connection":str,"link_type":str,'
        '"confidence":"high|medium|low","surfaceable":bool}],'
        '"synthesis_insight":str|null}\n'
        f"link_type must be one of: {sorted(_VALID_LINK_TYPES)}. "
        "Set surfaceable=true only when the connection meaningfully affects a "
        "decision. If no cross-pillar connection exists, return an empty list."
    )
    return base + framing


async def connect_cross_pillar(inputs: dict, db) -> dict:
    """
    inputs: ``{"nodes": [NodeSummary], "primary_intent": str, "session_id": str}``.
    Returns ``{"connections": [...], "synthesis_insight": str|null}``.
    """
    nodes = inputs.get("nodes") or []
    primary_intent = inputs.get("primary_intent", "")

    # Need at least two nodes from different pillars for a cross-pillar link.
    if len(nodes) < 2:
        return dict(_EMPTY)

    system = _system_prompt()
    user = json.dumps(
        {
            "primary_intent": primary_intent,
            "nodes": [
                {"id": str(n.get("id", "")), "title": n.get("title", ""),
                 "pillar": n.get("pillar", ""),
                 "content": (n.get("content", "") or "")[:300]}
                for n in nodes
            ],
        }
    )

    parsed = await call_json(system, user, logger=logger, max_tokens=1024, temperature=0.3)
    if parsed is None:
        return dict(_EMPTY)

    connections = []
    for c in parsed.get("connections") or []:
        link_type = c.get("link_type")
        if link_type not in _VALID_LINK_TYPES:
            link_type = LinkType.RELATED_TO.value      # safe default
        # A genuine cross-pillar connection must name two distinct pillars.
        if c.get("pillar_a") and c.get("pillar_b") and c.get("pillar_a") == c.get("pillar_b"):
            continue
        connections.append(
            {
                "node_id_a": str(c.get("node_id_a", "")),
                "node_id_b": str(c.get("node_id_b", "")),
                "pillar_a": c.get("pillar_a", ""),
                "pillar_b": c.get("pillar_b", ""),
                "connection": c.get("connection", ""),
                "link_type": link_type,
                "confidence": c.get("confidence", "low"),
                "surfaceable": bool(c.get("surfaceable", False)),
            }
        )

    return {
        "connections": connections,
        "synthesis_insight": parsed.get("synthesis_insight"),
    }
