"""
aether.skills.analytical.financial_net_worth
===============================================

SKILL-09 (Missing Specs). Computes net worth from explicit asset and
liability nodes in the personal_finance pillar. Rule-based (no LLM),
returns a dated snapshot with component breakdown.

INV-04 is applied by the read helper (speculative+pending_review nodes
excluded). Beyond that, the spec says speculative *amounts* must not be
used in the sum at all — so speculative-confidence nodes are excluded
from the totals and noted in ``missing_data_note``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aether.memory.read_protocol import read_pillar_nodes
from aether.models.enums import ConfidenceLevel, NodeType, PillarName

logger = logging.getLogger("aether.skills.analytical.financial_net_worth")

_AMOUNT_TYPES = [NodeType.FACT, NodeType.ARTIFACT]


def _parse_amount(raw) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


async def compute_net_worth(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "as_of_date": str|null}``.
    Returns the net-worth snapshot (see SKILL-09 outputs).
    """
    as_of = inputs.get("as_of_date") or datetime.now(timezone.utc).isoformat()

    nodes = await read_pillar_nodes([PillarName.PERSONAL_FINANCE], db, node_types=_AMOUNT_TYPES)

    assets, liabilities = [], []
    parse_errors = 0
    speculative_skipped = 0
    saw_inferred = False

    for node in nodes:
        meta = node.metadata_ or {}
        category = meta.get("category")
        if category not in ("asset", "liability"):
            continue
        # Do not use speculative amounts (spec); note their absence.
        if node.confidence == ConfidenceLevel.SPECULATIVE:
            speculative_skipped += 1
            continue
        amount = _parse_amount(meta.get("amount"))
        if amount is None:  # FM-02
            parse_errors += 1
            continue
        if node.confidence == ConfidenceLevel.INFERRED:
            saw_inferred = True
        entry = {
            "label": node.title,
            "amount": amount,
            "node_id": str(node.id),
            "confidence": node.confidence.value,
        }
        (assets if category == "asset" else liabilities).append(entry)

    total_assets = sum(e["amount"] for e in assets)
    total_liabilities = sum(e["amount"] for e in liabilities)
    node_count = len(assets) + len(liabilities)

    notes = []
    if node_count == 0:  # FM-01
        notes.append("No usable asset/liability nodes found; net worth is zero by default.")
    if parse_errors:  # FM-02
        notes.append(f"{parse_errors} node(s) had unparseable amounts and were skipped.")
    if speculative_skipped:
        notes.append(f"{speculative_skipped} speculative amount(s) excluded from the total.")

    # Confidence: high if all explicit, medium if any inferred, low if
    # (after excluding speculative) data is thin/absent.
    if node_count == 0:
        confidence = "low"
    elif saw_inferred:
        confidence = "medium"
    else:
        confidence = "high"

    return {
        "net_worth": total_assets - total_liabilities,  # FM-03: negative is valid
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "asset_breakdown": assets,
        "liability_breakdown": liabilities,
        "as_of_date": as_of,
        "confidence": confidence,
        "node_count_used": node_count,
        "missing_data_note": " ".join(notes) if notes else None,
    }
