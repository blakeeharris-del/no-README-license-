"""
aether.skills.executive.synthesis_diff_presenter
==================================================

SKILL-24 (Missing Specs). Formats a ``synthesis_runs.diff_report`` for
user review, separating auto-displayable inferred nodes from
speculative nodes that require explicit user confirmation.

Rule-based (no LLM). Reads the synthesis_runs row by id and categorizes
its diff_report. Phase-0 synthesis runs are stubs with an empty
diff_report; this returns an empty (but well-formed) review in that
case rather than raising.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from aether.models.logs import SynthesisRun

logger = logging.getLogger("aether.skills.executive.synthesis_diff_presenter")

_EMPTY = {
    "new_beliefs": [], "updated_beliefs": [], "contradictions": [],
    "superseded": [], "requires_confirmation": [],
    "summary_text": "No synthesis changes to review.", "total_changes": 0,
}


async def present_synthesis_diff(inputs: dict, db) -> dict:
    """
    inputs: ``{"synthesis_run_id": str, "session_id": str}``.
    Returns the structured diff review (see SKILL-24 outputs).
    """
    try:
        run_id = uuid.UUID(str(inputs["synthesis_run_id"]))
    except (ValueError, KeyError, TypeError):
        return dict(_EMPTY)

    run = (
        await db.execute(select(SynthesisRun).where(SynthesisRun.id == run_id))
    ).scalar_one_or_none()
    if run is None or not run.diff_report:
        return dict(_EMPTY)

    report = run.diff_report
    new_beliefs, updated_beliefs, contradictions, superseded, requires_confirmation = [], [], [], [], []

    for item in report.get("new_nodes", []) or []:
        entry = {"node_id": str(item.get("node_id", "")), "title": item.get("title", ""),
                 "confidence": item.get("confidence", "inferred")}
        # Only speculative nodes require explicit confirmation; inferred
        # nodes are auto-displayable in new_beliefs.
        if item.get("confidence") == "speculative":
            requires_confirmation.append({
                "node_id": entry["node_id"], "title": entry["title"],
                "content": item.get("content", ""),
            })
        else:
            new_beliefs.append(entry)

    for item in report.get("updated_nodes", []) or []:
        updated_beliefs.append({
            "node_id": str(item.get("node_id", "")), "title": item.get("title", ""),
            "old_title": item.get("old_title", ""),
        })
    for item in report.get("contradictions", []) or []:
        contradictions.append({
            "node_id_a": str(item.get("node_id_a", "")),
            "node_id_b": str(item.get("node_id_b", "")),
            "description": item.get("description", ""),
        })
    for item in report.get("superseded", []) or []:
        superseded.append({"node_id": str(item.get("node_id", "")), "title": item.get("title", "")})

    total_changes = (len(new_beliefs) + len(updated_beliefs) + len(contradictions)
                     + len(superseded) + len(requires_confirmation))
    summary_text = (
        f"Aether updated {len(new_beliefs) + len(updated_beliefs)} belief(s). "
        f"Review needed for {len(requires_confirmation)} speculative item(s)."
        if total_changes else "No synthesis changes to review."
    )

    return {
        "new_beliefs": new_beliefs,
        "updated_beliefs": updated_beliefs,
        "contradictions": contradictions,
        "superseded": superseded,
        "requires_confirmation": requires_confirmation,
        "summary_text": summary_text,
        "total_changes": total_changes,
    }
