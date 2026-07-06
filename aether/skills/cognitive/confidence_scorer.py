"""
aether.skills.cognitive.confidence_scorer
============================================

SKILL-03 (Missing Specs). Rule-based (no LLM) evaluation of a node
draft's evidentiary basis, returning a confidence classification plus
the count of corroborating nodes. Used by the synthesis engine before
L3 nodes are written.

Enforces INV-03 at the classification layer: a synthesis/agent_write
node can never be classified "explicit" — only user_explicit and
sync_import sources earn "explicit".
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from aether.models.enums import ConfidenceLevel, NodeSource
from aether.models.nodes import Node

logger = logging.getLogger("aether.skills.cognitive.confidence_scorer")

_EXPLICIT_SOURCES = {NodeSource.USER_EXPLICIT.value, NodeSource.SYNC_IMPORT.value}


def _coerce_uuids(raw_ids) -> list[uuid.UUID]:
    """FM-01: skip invalid UUIDs, keep the valid ones."""
    valid: list[uuid.UUID] = []
    for rid in raw_ids or []:
        try:
            valid.append(uuid.UUID(str(rid)))
        except (ValueError, AttributeError, TypeError):
            continue
    return valid


async def score_confidence(inputs: dict, db) -> dict:
    """
    inputs: ``{"node_draft": {"title", "content", "source"},
               "supporting_node_ids": [str], "pillar": str}``.
    Returns ``{"confidence", "support_count", "rationale", "demotion_risk"}``.
    """
    draft = inputs.get("node_draft", {}) or {}
    source = draft.get("source")
    valid_ids = _coerce_uuids(inputs.get("supporting_node_ids"))

    # Look up the real supporting nodes: support_count is the number of
    # *existing* corroborating nodes, and demotion_risk is true if any
    # supporting node is itself speculative (INV-04 propagation warning).
    supporting: list[Node] = []
    if valid_ids:
        supporting = list(
            (await db.execute(select(Node).where(Node.id.in_(valid_ids)))).scalars().all()
        )
    support_count = len(supporting)
    demotion_risk = any(n.confidence == ConfidenceLevel.SPECULATIVE for n in supporting)

    # ---- classification rules (rule-based, no LLM) ----------------------
    if source in _EXPLICIT_SOURCES:
        confidence = ConfidenceLevel.EXPLICIT.value
        rationale = f"Source '{source}' is user-authoritative; classified explicit."
    elif source == NodeSource.SYNTHESIS.value:
        if support_count >= 3:
            confidence = ConfidenceLevel.INFERRED.value
            rationale = f"{support_count} corroborating nodes support this synthesis."
        else:
            confidence = ConfidenceLevel.SPECULATIVE.value
            rationale = (
                f"Only {support_count} corroborating node(s) (<3); synthesis is speculative."
            )
    elif source == NodeSource.AGENT_WRITE.value:
        # INV-03: agent_write may never be explicit; capped at inferred.
        confidence = ConfidenceLevel.INFERRED.value
        rationale = "Agent-written node; capped at inferred per INV-03."
    else:
        # FM-02: source not in enum -> speculative.
        confidence = ConfidenceLevel.SPECULATIVE.value
        rationale = f"Unrecognized source {source!r}; defaulting to speculative (FM-02)."

    return {
        "confidence": confidence,
        "support_count": support_count,
        "rationale": rationale,
        "demotion_risk": demotion_risk,
    }
