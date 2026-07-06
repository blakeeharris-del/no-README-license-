"""
aether.skills.invoker
========================

``invoke_skill()`` — the single entry point for all skill calls
(Phase-0 Prompt Section 13). Enforces INV-09: every invocation is
logged to ``skill_invocation_log`` on START and on
COMPLETE/ERROR/TIMEOUT.
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.config import settings
from aether.invariants.guards import (
    SkillExecutionError,
    SkillNotActiveError,
    SkillNotFoundError,
    SkillTimeoutError,
    compute_hash,
)
from aether.models.enums import SkillStatus
from aether.models.runtime import Skill as SkillModel
from aether.models.runtime import SkillInvocationLog
from aether.schemas.skills import SkillResult

logger = logging.getLogger("aether.skills.invoker")


async def invoke_skill(
    skill_name: str,
    inputs: dict,
    session_id: UUID,
    invoked_by: str,
    loop_run_id: UUID | None,
    db: AsyncSession,
) -> SkillResult:
    """Invoke a skill by name through the registry, with full INV-09 logging."""

    # ---- STEP 1: inputs hash -------------------------------------------
    inputs_hash = compute_hash(inputs)

    # ---- STEP 2: INSERT running log row [INV-09 START] -------------------
    log_entry = SkillInvocationLog(
        skill_name=skill_name,
        invoked_by=invoked_by,
        session_id=session_id,
        loop_run_id=loop_run_id,
        inputs_hash=inputs_hash,
        status="running",
    )
    db.add(log_entry)
    await db.flush()
    await db.commit()
    log_id = log_entry.id

    # ---- STEP 3: registry lookup ----------------------------------------
    from aether.skills.registry import SKILL_REGISTRY, SKILL_TIMEOUTS

    skill_fn = SKILL_REGISTRY.get(skill_name)
    if skill_fn is None:
        await _update_log(db, log_id, status="error", error_detail="skill_not_found")
        raise SkillNotFoundError(skill_name)

    # ---- STEP 3b: Active-status gate [Foundation §10.7.1] ----------------
    # "No skill may be invoked unless it is Active." The skills table is
    # the lifecycle system of record (Impl Plan §8.3); a skill is
    # invocable only if it has an Active row there. Safety skills bypass
    # invoke_skill entirely, so this gate only governs registry-dispatched
    # skills.
    statuses = [
        r[0]
        for r in (
            await db.execute(
                select(SkillModel.status).where(SkillModel.name == skill_name)
            )
        ).all()
    ]
    if SkillStatus.ACTIVE not in statuses:
        current = statuses[0].value if statuses else None
        await _update_log(db, log_id, status="error", error_detail="skill_not_active")
        raise SkillNotActiveError(skill_name, current)

    # ---- STEP 4: timeout ---------------------------------------------------
    timeout_ms = SKILL_TIMEOUTS.get(skill_name, settings.default_skill_timeout_ms)

    # ---- STEP 5: execute with timeout -----------------------------------
    start = time.monotonic()
    try:
        output = await asyncio.wait_for(skill_fn(inputs, db), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        await _update_log(db, log_id, status="timeout")
        raise SkillTimeoutError(skill_name, timeout_ms) from None
    except Exception as exc:
        await _update_log(db, log_id, status="error", error_detail=str(exc)[:2000])
        raise SkillExecutionError(skill_name, str(exc)) from exc

    # ---- STEP 6: latency + output hash -----------------------------------
    latency_ms = int((time.monotonic() - start) * 1000)
    outputs_hash = compute_hash(output) if output else None

    # ---- STEP 7: mark complete [INV-09 COMPLETE] --------------------------
    await _update_log(
        db, log_id, status="ok", outputs_hash=outputs_hash, latency_ms=latency_ms
    )

    # ---- STEP 8 -------------------------------------------------------------
    return SkillResult(status="ok", output=output, latency_ms=latency_ms, log_id=log_id)


async def _update_log(
    db: AsyncSession,
    log_id: UUID,
    *,
    status: str,
    outputs_hash: str | None = None,
    latency_ms: int | None = None,
    error_detail: str | None = None,
) -> None:
    """
    Updates the skill_invocation_log row in its own transaction. A
    separate commit from the row's creation, since the surrounding
    invocation may be raising an exception at the point this is called
    (timeout/error paths) — the log update must survive regardless of
    what the caller does with that exception afterward.
    """
    row = (
        await db.execute(select(SkillInvocationLog).where(SkillInvocationLog.id == log_id))
    ).scalar_one()
    row.status = status
    if outputs_hash is not None:
        row.outputs_hash = outputs_hash
    if latency_ms is not None:
        row.latency_ms = latency_ms
    if error_detail is not None:
        row.error_detail = error_detail
    await db.commit()
