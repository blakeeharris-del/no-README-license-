"""
aether.skills.executive.session_briefer
==========================================

SKILL-20 (Missing Specs). Produces the session brief from L1 Working
Memory: priorities, this-week deadlines, pending-approval/synthesis/
contradiction/flagged counts, and an LLM-written narrative.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from aether.models.enums import EscalationStatus
from aether.models.runtime import PendingEscalation
from aether.skills._llm import call_json

logger = logging.getLogger("aether.skills.executive.session_briefer")

_BASE_PROMPT = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "base_system.txt"


async def build_session_brief(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "l1": L1WorkingMemory dict}``.
    Returns the session brief (see SKILL-20 outputs).
    """
    session_id = inputs.get("session_id")
    l1 = inputs.get("l1") or {}

    open_tasks = l1.get("open_tasks") or []
    upcoming = l1.get("upcoming_deadlines") or []
    flagged = l1.get("flagged_nodes") or []
    contradiction_count = int(l1.get("contradiction_count") or 0)
    synthesis_pending = int(l1.get("pending_reviews") or 0)

    # Priorities: flagged nodes and near-term deadlines.
    priorities = [{"title": f.get("title", ""), "priority_class": "p1", "source": "flagged"}
                  for f in flagged[:5]]
    now = datetime.now(timezone.utc)
    deadlines_this_week = []
    for d in upcoming:
        try:
            dt = datetime.fromisoformat(d.get("deadline", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (dt - now).days <= 7:
                deadlines_this_week.append(d)
                priorities.append({"title": d.get("title", ""), "priority_class": "p1",
                                   "source": "deadline"})
        except (ValueError, TypeError):
            continue

    pending_approvals = 0
    if session_id:
        pending_approvals = (
            await db.execute(
                select(func.count()).select_from(PendingEscalation).where(
                    PendingEscalation.session_id == session_id,
                    PendingEscalation.status == EscalationStatus.PENDING,
                )
            )
        ).scalar_one()

    structured = {
        "priorities": priorities,
        "deadlines_this_week": deadlines_this_week,
        "pending_approvals": int(pending_approvals),
        "synthesis_pending": synthesis_pending,
        "contradiction_count": contradiction_count,
        "flagged_count": len(flagged),
    }

    # FM-01: brand-new user / empty L1.
    if not (open_tasks or upcoming or flagged or pending_approvals):
        structured["brief_text"] = "No active items yet — this is a fresh start. Add context and Aether will begin tracking it."
        return structured

    system = _BASE_PROMPT.read_text() + (
        "\n\nTASK: Write a 2-4 sentence plain-text session brief from this data. "
        'Return ONLY JSON: {"brief_text": str}'
    )
    parsed = await call_json(system, json.dumps(structured), logger=logger,
                             max_tokens=256, temperature=0.3)
    # FM-02: LLM timeout -> structured data with fallback text.
    structured["brief_text"] = (parsed or {}).get("brief_text") or "Unable to generate summary."
    return structured
