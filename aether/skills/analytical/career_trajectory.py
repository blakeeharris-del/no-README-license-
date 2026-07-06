"""
aether.skills.analytical.career_trajectory
=============================================

SKILL-10 (Missing Specs). Infers career trajectory from role,
credential, and opportunity nodes via an LLM call. Output is ALWAYS
labeled "inferred" — never presented as established fact — and cites
evidence node ids.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aether.memory.read_protocol import read_pillar_nodes
from aether.models.enums import NodeStatus, PillarName
from aether.skills._llm import call_json

logger = logging.getLogger("aether.skills.analytical.career_trajectory")

_CAREER_PROMPT = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "pillar" / "career.txt"
_DIRECTIONS = {"ascending", "lateral", "pivoting", "unclear"}


async def analyze_career_trajectory(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "include_speculative": bool}``.
    Returns the trajectory analysis (see SKILL-10 outputs); confidence
    is always "inferred".
    """
    nodes = await read_pillar_nodes([PillarName.CAREER], db)

    evidence = [str(n.id) for n in nodes]
    open_opportunities = sum(
        1 for n in nodes
        if (n.metadata_ or {}).get("category") == "opportunity" and n.status == NodeStatus.ACTIVE
    )

    empty = {
        "trajectory_summary": "Insufficient career data to infer a trajectory.",
        "current_role": None,
        "trajectory_direction": "unclear",
        "evidence_node_ids": evidence,
        "confidence": "inferred",
        "open_opportunities": open_opportunities,
        "credential_gaps": None,
    }
    if not nodes:
        return empty

    system = _CAREER_PROMPT.read_text() + (
        "\n\nTASK: Infer career trajectory from these nodes. This is an INFERENCE, "
        "never established fact. Return ONLY JSON:\n"
        '{"trajectory_summary":str,"current_role":str|null,'
        '"trajectory_direction":"ascending|lateral|pivoting|unclear",'
        '"credential_gaps":[str]|null}\n'
        "Base every statement only on the provided nodes."
    )
    user = json.dumps({
        "nodes": [{"id": str(n.id), "title": n.title, "content": (n.content or "")[:400],
                   "created_at": n.created_at.isoformat()} for n in nodes],
    })

    parsed = await call_json(system, user, logger=logger, max_tokens=512, temperature=0.3)
    if parsed is None:
        return empty

    direction = parsed.get("trajectory_direction")
    if direction not in _DIRECTIONS:
        direction = "unclear"

    return {
        "trajectory_summary": parsed.get("trajectory_summary", empty["trajectory_summary"]),
        "current_role": parsed.get("current_role"),
        "trajectory_direction": direction,
        "evidence_node_ids": evidence,      # always cite evidence
        "confidence": "inferred",           # spec: never explicit
        "open_opportunities": open_opportunities,
        "credential_gaps": parsed.get("credential_gaps"),
    }
