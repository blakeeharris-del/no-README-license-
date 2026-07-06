"""
aether.agents.sub_agents.handlers._common
============================================

Shared helpers for the 30 sub-agent handlers: invoking a skill (which
produces real skill_invocation_log rows, feeding EC-19/20), reading
pillar nodes with metadata, and deadline math.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from aether.memory.read_protocol import read_pillar_nodes
from aether.models.enums import PillarName


async def call_skill(name: str, inputs: dict, db, session_id, invoked_by: str) -> dict:
    """Invoke a skill through the registry (real logging + Active-gate)."""
    from aether.skills.invoker import invoke_skill

    result = await invoke_skill(name, inputs, _uuid(session_id), invoked_by, None, db)
    return result.output


def _uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def nodes_for(pillar: PillarName, db, *, category=None, node_types=None) -> list:
    """INV-04-filtered ORM nodes for a pillar, optionally by metadata category."""
    nodes = await read_pillar_nodes([pillar], db, node_types=node_types)
    if category is not None:
        cats = category if isinstance(category, (set, list, tuple)) else {category}
        nodes = [n for n in nodes if (n.metadata_ or {}).get("category") in cats]
    return nodes


def days_until(iso: str | None, now: datetime | None = None) -> int | None:
    if not iso:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - now).days
    except (ValueError, TypeError):
        return None


def priority_for_days(du: int | None, *, p0_within=1, p1_within=7) -> str:
    """Simple deadline priority: overdue/very-near -> p0, near -> p1, else p3."""
    if du is None:
        return "p3"
    if du < 0 or du <= p0_within:
        return "p0"
    if du <= p1_within:
        return "p1"
    return "p3"
