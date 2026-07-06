"""
aether.agents.sub_agents.runtime
==================================

The execution wrapper for all 30 sub-agents. Every sub-agent invocation
goes through ``run_sub_agent``, which:

  * resolves the sub_agents registry row,
  * inserts a ``sub_agent_runs`` row (EC-18: sub-agent invocations are
    logged to sub_agent_runs, NOT action_log — Foundation §10.5),
  * runs the handler under the spec's max_duration timeout,
  * records terminal status (completed / failed / force_terminated) with
    a result summary,
  * returns a typed ``SubAgentResult`` envelope (principle 7).

Sub-agents are stateless (principle 6): all state arrives via ``inputs``.
Handlers never raise across the tier boundary — a handler error is
captured as status='failed' and returned to the parent, which is itself
the escalation signal.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.agents.sub_agents.catalog import SUB_AGENT_CATALOG
from aether.models.enums import SubAgentStatus
from aether.models.runtime import SubAgent, SubAgentRun

logger = logging.getLogger("aether.agents.sub_agents.runtime")


class SubAgentNotFoundError(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"Sub-agent not found / not seeded: {name}")


@dataclass
class SubAgentResult:
    name: str
    output: dict[str, Any]
    run_id: UUID
    status: str  # SubAgentStatus value


def _summarize(output: dict) -> dict:
    """Compact result summary for the run row (avoid storing huge payloads)."""
    summary: dict[str, Any] = {}
    for k, v in (output or {}).items():
        if isinstance(v, list):
            summary[f"{k}_count"] = len(v)
        elif isinstance(v, (int, float, bool, str)) or v is None:
            summary[k] = v
    return summary


async def run_sub_agent(
    name: str,
    inputs: dict,
    session_id: UUID,
    db: AsyncSession,
    *,
    loop_run_id: UUID | None = None,
) -> SubAgentResult:
    spec = SUB_AGENT_CATALOG.get(name)
    if spec is None:
        raise SubAgentNotFoundError(name)

    sub_agent_id = (
        await db.execute(select(SubAgent.id).where(SubAgent.name == name))
    ).scalar_one_or_none()
    if sub_agent_id is None:
        raise SubAgentNotFoundError(name)

    # ---- log spawn (EC-18: sub_agent_runs, not action_log) --------------
    run = SubAgentRun(
        sub_agent_id=sub_agent_id,
        session_id=session_id,
        loop_run_id=loop_run_id,
        parent_agent=spec.parent_agent,
        status=SubAgentStatus.RUNNING,
    )
    db.add(run)
    await db.flush()
    run_id = run.id

    # ---- lazy handler lookup (avoids import cycle) ----------------------
    from aether.agents.sub_agents.handlers import SUB_AGENT_HANDLERS

    handler = SUB_AGENT_HANDLERS.get(name)
    if handler is None:
        run.status = SubAgentStatus.FAILED
        run.terminated_at = datetime.now(timezone.utc)
        run.error_detail = "no handler registered"
        await db.flush()
        return SubAgentResult(name, {"error": "no handler"}, run_id, SubAgentStatus.FAILED.value)

    timeout_s = spec.max_duration_ms / 1000
    try:
        output = await asyncio.wait_for(handler(inputs, db), timeout=timeout_s)
        run.status = SubAgentStatus.COMPLETED
        run.result_summary = _summarize(output)
    except asyncio.TimeoutError:
        run.status = SubAgentStatus.FORCE_TERMINATED
        run.error_detail = f"exceeded max_duration {spec.max_duration_ms}ms"
        output = {"error": "timeout", "truncated": True}
        logger.warning("sub-agent %s force-terminated (timeout)", name)
    except Exception as exc:  # tier boundary: never propagate
        run.status = SubAgentStatus.FAILED
        run.error_detail = str(exc)[:500]
        output = {"error": str(exc)}
        logger.exception("sub-agent %s failed", name)

    run.terminated_at = datetime.now(timezone.utc)
    await db.flush()
    return SubAgentResult(name, output, run_id, run.status.value)
