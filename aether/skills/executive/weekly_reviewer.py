"""
aether.skills.executive.weekly_reviewer
==========================================

SKILL-21 (Missing Specs). Produces the weekly review: per-pillar
snapshots, activity counts since the last review, open loops, focus
recommendations, and an LLM narrative.

Implementation note / flagged simplification: the spec's Implementation
Notes suggest building pillar_snapshots "from each analytical skill
(called in parallel)". The OUTPUT contract is
``{status, key_items, alerts}`` per pillar; this builds that faithfully
from direct per-pillar reads plus the pending-escalation count (alerts),
which keeps the skill self-contained and fast. Wiring the six analytical
skills in is a clean future enhancement that does not change the output
shape.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from aether.memory.read_protocol import read_by_pillar
from aether.models.enums import EscalationStatus, PillarName
from aether.models.logs import ActionLog
from aether.models.nodes import Node
from aether.models.runtime import PendingEscalation
from aether.skills._llm import call_json

logger = logging.getLogger("aether.skills.executive.weekly_reviewer")

_BASE_PROMPT = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "base_system.txt"


def _parse_since(raw) -> datetime:
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    from datetime import timedelta
    return datetime.now(timezone.utc) - timedelta(days=7)


async def build_weekly_review(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "since_date": str}``.
    Returns the weekly review (see SKILL-21 outputs).
    """
    since = _parse_since(inputs.get("since_date"))

    # Activity counts.
    nodes_added = (
        await db.execute(select(func.count()).select_from(Node).where(Node.created_at > since))
    ).scalar_one()
    decisions_made = (
        await db.execute(
            select(func.count()).select_from(ActionLog).where(
                ActionLog.timestamp > since, ActionLog.user_confirmed.is_(True)
            )
        )
    ).scalar_one()

    # Per-pillar snapshots.
    pillar_snapshots = {}
    open_loops: list[str] = []
    for pillar in PillarName:
        summaries = await read_by_pillar([pillar], db)
        alerts = (
            await db.execute(
                select(func.count()).select_from(PendingEscalation).where(
                    PendingEscalation.status == EscalationStatus.PENDING,
                    PendingEscalation.content["pillar"].astext == pillar.value,
                )
            )
        ).scalar_one()
        key_items = [s.title for s in summaries[:3]]
        status = "active" if summaries else "quiet"
        pillar_snapshots[pillar.value] = {
            "status": status, "key_items": key_items, "alerts": int(alerts)
        }

    # Open loops: pending escalations (titles).
    pending = (
        await db.execute(
            select(PendingEscalation).where(PendingEscalation.status == EscalationStatus.PENDING)
        )
    ).scalars().all()
    open_loops = [(e.content or {}).get("title", "escalation") for e in pending][:10]

    # LLM narrative + focus recommendations.
    structured = {
        "pillar_snapshots": pillar_snapshots,
        "nodes_added_this_week": int(nodes_added),
        "decisions_made": int(decisions_made),
        "open_loops": open_loops,
    }
    system = _BASE_PROMPT.read_text() + (
        "\n\nTASK: Write a 200-400 word weekly review and 3-5 focus recommendations "
        'from this data. Return ONLY JSON: {"review_text": str, "focus_recommendations": [str]}'
    )
    parsed = await call_json(system, json.dumps(structured), logger=logger,
                             max_tokens=1024, temperature=0.3)
    parsed = parsed or {}

    return {
        "pillar_snapshots": pillar_snapshots,
        "nodes_added_this_week": int(nodes_added),
        "decisions_made": int(decisions_made),
        "open_loops": open_loops,
        "focus_recommendations": parsed.get("focus_recommendations") or [],
        "review_text": parsed.get("review_text") or "Weekly review narrative unavailable.",
    }
