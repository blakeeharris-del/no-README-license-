"""
aether.skills.operational.session_initializer
==================================================

SKILL-18 (Phase-0 Prompt Section 15). Thin skill wrapper delegating to
``rebuild_l1()`` (Section 12 / ``aether/memory/session_state.py``).
"""

from __future__ import annotations

from aether.memory.session_state import rebuild_l1


async def initialize_session(inputs: dict, db) -> dict:
    """inputs: ``{'session_id': str}``. Returns an ``L1WorkingMemory`` dict."""
    l1 = await rebuild_l1(inputs["session_id"], db)
    return l1.model_dump(mode="json")
