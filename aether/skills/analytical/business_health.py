"""
aether.skills.analytical.business_health
==========================================

SKILL-11 (Missing Specs). Summarizes business-venture health into a
structured scorecard. The countable structure (pipeline, obligations,
node count, last_updated) is computed rule-based; the LLM supplies
revenue_status, strategic_gaps, and the stable-vs-strong judgment.

overall_health rules (spec):
  overdue_obligations > 2 OR pipeline.at_risk > pipeline.active -> "at_risk"
  health_node_count < 5                                          -> "unknown"
  else                                                           -> LLM judgment
                                                                    ("stable"|"strong")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from aether.memory.read_protocol import read_pillar_nodes
from aether.models.enums import NodeStatus, NodeType, PillarName
from aether.skills._llm import call_json

logger = logging.getLogger("aether.skills.analytical.business_health")

_BUSINESS_PROMPT = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "pillar" / "business.txt"


def _is_overdue(meta: dict, now: datetime) -> bool:
    dl = meta.get("deadline")
    if not dl:
        return False
    try:
        dt = datetime.fromisoformat(dl)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < now
    except (ValueError, TypeError):
        return False


async def assess_business_health(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "business_name": str|null}``.
    Returns the business health scorecard (see SKILL-11 outputs).
    """
    business_name = inputs.get("business_name")
    now = datetime.now(timezone.utc)

    nodes = await read_pillar_nodes([PillarName.BUSINESS], db)
    if business_name:
        nodes = [n for n in nodes if (n.metadata_ or {}).get("business_name") == business_name]

    # ---- rule-based structure -------------------------------------------
    pipeline = {"active": 0, "at_risk": 0, "closing": 0}
    open_obligations = 0
    overdue_obligations = 0
    last_updated = None
    has_revenue = False

    for n in nodes:
        meta = n.metadata_ or {}
        cat = meta.get("category")
        if cat == "client":
            stage = meta.get("stage", "active")
            if stage in pipeline:
                pipeline[stage] += 1
        if cat == "revenue":
            has_revenue = True
        if n.type in (NodeType.TASK, NodeType.FACT) and meta.get("deadline"):
            if n.status == NodeStatus.ACTIVE:
                open_obligations += 1
                if _is_overdue(meta, now):
                    overdue_obligations += 1
        if last_updated is None or n.created_at > last_updated:
            last_updated = n.created_at

    health_node_count = len(nodes)

    # ---- overall_health per spec rules ----------------------------------
    if health_node_count == 0:
        overall_health = "unknown"
    elif overdue_obligations > 2 or pipeline["at_risk"] > pipeline["active"]:
        overall_health = "at_risk"
    elif health_node_count < 5:
        overall_health = "unknown"
    else:
        overall_health = None  # decided by LLM below

    # ---- LLM: revenue_status, strategic_gaps, stable/strong -------------
    revenue_status = None
    strategic_gaps: list[str] = []
    confidence = "insufficient_data" if health_node_count < 5 else "medium"

    if nodes:
        system = _BUSINESS_PROMPT.read_text() + (
            "\n\nTASK: Assess venture health from these nodes. Return ONLY JSON:\n"
            '{"revenue_status":str|null,"strategic_gaps":[str],'
            '"health_judgment":"stable|strong|at_risk|unknown"}\n'
            "Only use facts present in the nodes."
        )
        user = json.dumps({
            "has_revenue_nodes": has_revenue,
            "pipeline": pipeline,
            "overdue_obligations": overdue_obligations,
            "nodes": [{"title": n.title, "content": (n.content or "")[:300]} for n in nodes],
        })
        parsed = await call_json(system, user, logger=logger, max_tokens=512, temperature=0.3)
        if parsed is not None:
            revenue_status = parsed.get("revenue_status")
            strategic_gaps = parsed.get("strategic_gaps") or []
            if overall_health is None:  # only the stable/strong slot defers to LLM
                judgment = parsed.get("health_judgment")
                overall_health = judgment if judgment in ("stable", "strong") else "stable"
                confidence = "high" if has_revenue else "medium"

    if overall_health is None:
        overall_health = "stable" if has_revenue else "unknown"

    return {
        "overall_health": overall_health,
        "revenue_status": revenue_status,
        "client_pipeline": pipeline,
        "open_obligations": open_obligations,
        "overdue_obligations": overdue_obligations,
        "strategic_gaps": strategic_gaps,
        "health_node_count": health_node_count,
        "confidence": confidence,
        "last_updated": last_updated.isoformat() if last_updated else None,
    }
