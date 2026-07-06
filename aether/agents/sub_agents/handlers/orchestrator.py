"""
Orchestrator sub-agent handlers (SA-28, 29, 30). These report to the
master agent, not a specialist. SA-30 is the Synthesis Coordinator
(Foundation §10.4): it routes structured cross-pillar signals to the
master and does NOT produce final analysis (EC-23), and it is the path
through which cross_pillar_connector's signal reaches the master (EC-22).
"""

from __future__ import annotations

import logging

from aether.agents.sub_agents.handlers._common import call_skill
from aether.memory.read_protocol import read_by_pillar
from aether.memory.synthesis import run_synthesis
from aether.models.enums import PillarName

logger = logging.getLogger("aether.agents.sub_agents.orchestrator")

_TOKEN_BUDGET_NODES = 40  # simple node-count proxy for the token budget


async def multi_pillar_collector(inputs: dict, db) -> dict:
    """SA-28: gather nodes from 2+ pillars, dedupe, INV-04 filter, budget."""
    pillar_values = inputs.get("pillars") or []
    pillars = [PillarName(p) for p in pillar_values if p in PillarName._value2member_map_]

    seen, merged, per_pillar = set(), [], {}
    for pillar in pillars:
        summaries = await read_by_pillar([pillar], db)  # INV-04 filtered
        per_pillar[pillar.value] = len(summaries)
        for s in summaries:
            if s.id in seen:
                continue
            seen.add(s.id)
            merged.append({"id": str(s.id), "title": s.title,
                           "content": (s.content or "")[:300],
                           "pillar": pillar.value, "confidence": s.confidence.value})

    total = len(merged)
    truncated = total > _TOKEN_BUDGET_NODES
    return {"merged_nodes": merged[:_TOKEN_BUDGET_NODES],
            "per_pillar_counts": per_pillar, "truncated": truncated,
            "total_before_truncation": total}


async def decision_assembler(inputs: dict, db) -> dict:
    """SA-29: assemble a full multi-pillar decision brief."""
    pillars = inputs.get("pillars_affected") or []
    collected = await multi_pillar_collector(
        {"pillars": pillars, "session_id": inputs["session_id"]}, db)
    brief = await call_skill(
        "executive.decision_protocol",
        {"proposed_action": inputs.get("proposed_action", ""),
         "relevant_nodes": collected["merged_nodes"],
         "user_intent": inputs.get("intent", {}),
         "session_id": inputs["session_id"]},
        db, inputs["session_id"], "orchestrator.decision_assembler")
    return brief


async def synthesis_coordinator(inputs: dict, db) -> dict:
    """
    SA-30 / Foundation §10.4. Coordinates the synthesis cycle: manages
    the advisory lock via run_synthesis(), routes cross-pillar signals
    from cross_pillar_connector to the master, and returns a structured
    diff report. Does NOT produce final analysis (EC-23).
    """
    session_id = inputs["session_id"]
    triggered_by = inputs.get("triggered_by", "session.closed")

    # Manage the advisory lock. If held, skip cleanly (no error).
    run = await run_synthesis(triggered_by, db)
    if run is None:
        return {"skipped": True, "reason": "advisory lock held by concurrent run",
                "cross_pillar_signals": [], "diff": None}

    # Gather multi-pillar nodes and route them through cross_pillar_connector
    # — this is how the cross-pillar signal (EC-22) reaches the master.
    collected = await multi_pillar_collector(
        {"pillars": [p.value for p in PillarName], "session_id": session_id}, db)
    connector_out = await call_skill(
        "cognitive.cross_pillar_connector",
        {"nodes": collected["merged_nodes"], "primary_intent": "synthesis",
         "session_id": str(session_id)},
        db, session_id, "orchestrator.synthesis_coordinator")
    cross_pillar_signals = connector_out.get("connections", [])

    # Record the signals on the synthesis_runs diff_report and present them.
    run.diff_report = {
        "cross_pillar_signals": cross_pillar_signals,
        "synthesis_insight": connector_out.get("synthesis_insight"),
        "new_nodes": [], "updated_nodes": [], "contradictions": [], "superseded": [],
    }
    await db.flush()

    diff = await call_skill(
        "executive.synthesis_diff_presenter",
        {"synthesis_run_id": str(run.id), "session_id": str(session_id)},
        db, session_id, "orchestrator.synthesis_coordinator")

    # Structured signals only — final analysis is the master's job (EC-23).
    return {
        "skipped": False,
        "synthesis_run_id": str(run.id),
        "cross_pillar_signals": cross_pillar_signals,
        "diff": diff,
    }
