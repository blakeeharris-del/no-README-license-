"""
aether.skills.executive.approval_presenter
=============================================

SKILL-23 (Missing Specs). Formats an ApprovalRequest for the user:
human-readable prompt plus the confirmation wording required for the
action's risk level. Rule-based (no LLM).

Discrepancy B note: catalog tags this Phase-0, but it did not exist in
the Phase-0 code. Resolved to Phase-1 per governance precedence.

Risk level -> confirmation requirement (spec):
  low        -> "yes/no"
  medium     -> "type CONFIRM"
  high       -> "type the action name exactly"
  restricted -> "type CONFIRM plus reason"   (legal/tax/financial/employment)
"""

from __future__ import annotations

import logging

from aether.schemas.agent import ApprovalRequest

logger = logging.getLogger("aether.skills.executive.approval_presenter")

_CONFIRMATION = {
    "low": "yes/no",
    "medium": "type CONFIRM",
    "high": "type the action name exactly",
    "restricted": "type CONFIRM plus reason",
}


async def present_approval(inputs: dict, db) -> dict:
    """
    inputs: ``{"action","target","amount_or_consequence","timing",
               "authority_level","risk_level"}``.
    Returns ``{"approval_text","approval_request","confirmation_required"}``.
    """
    risk_level = inputs.get("risk_level", "medium")
    if risk_level not in _CONFIRMATION:
        risk_level = "medium"

    request = ApprovalRequest(
        action=inputs.get("action", ""),
        target=inputs.get("target", ""),
        amount_or_consequence=inputs.get("amount_or_consequence", ""),
        timing=inputs.get("timing", ""),
        authority_level=int(inputs.get("authority_level", 3)),
        risk_level=risk_level,
    )
    confirmation_required = _CONFIRMATION[risk_level]

    approval_text = (
        f"Approval requested: {request.action} — {request.target}.\n"
        f"Consequence: {request.amount_or_consequence}\n"
        f"Timing: {request.timing}\n"
        f"Risk level: {risk_level}.\n"
        f"To proceed, {confirmation_required}."
    )

    return {
        "approval_text": approval_text,
        "approval_request": request.model_dump(mode="json"),
        "confirmation_required": confirmation_required,
    }
