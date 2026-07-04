"""
aether.api.routes.session
============================

The 4 Phase-0 endpoints (Phase-0 Prompt Section 19).
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aether.config import settings
from aether.database import get_db
from aether.invariants.guards import InvariantViolation
from aether.loops.goal_loop import GoalLoop
from aether.memory.session_state import rebuild_l1, save_l1_snapshot
from aether.memory.synthesis import run_synthesis
from aether.models.enums import (
    ActionType,
    AgentName,
    EscalationStatus,
    EscalationType,
    LoopStatus,
    PriorityClass,
    SessionStatus,
)
from aether.models.logs import ActionLog, SynthesisRun
from aether.models.nodes import Node
from aether.models.runtime import LoopRun, PendingEscalation
from aether.models.sessions import Session
from aether.schemas.agent import AgentResponse
from aether.schemas.gateway import GatewayResult
from aether.schemas.session import L1WorkingMemory, SessionOpenResponse
from aether.skills.invoker import invoke_skill

logger = logging.getLogger("aether.api.routes.session")

router = APIRouter(prefix="/session", tags=["session"])


class InputRequest(BaseModel):
    user_input: str


class ApproveRequestBody(BaseModel):
    approved: bool
    action_log_id: UUID


async def _get_active_session(session_id: UUID, db: AsyncSession) -> Session:
    session_row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one_or_none()
    if session_row is None or session_row.status != SessionStatus.ACTIVE:
        raise HTTPException(status_code=404, detail="Session not found or not active")
    return session_row


async def _get_active_loop_run_id(session_id: UUID, db: AsyncSession) -> UUID:
    loop_run = (
        await db.execute(
            select(LoopRun)
            .where(LoopRun.session_id == session_id, LoopRun.status == LoopStatus.RUNNING)
            .order_by(LoopRun.start_time.desc())
        )
    ).scalars().first()
    if loop_run is None:
        raise HTTPException(status_code=404, detail="No active loop_run for this session")
    return loop_run.id


@router.post("/start", response_model=SessionOpenResponse)
async def start_session(db: AsyncSession = Depends(get_db)) -> SessionOpenResponse:
    session_row = Session(status=SessionStatus.ACTIVE)
    db.add(session_row)
    await db.commit()
    session_id = session_row.id

    await invoke_skill(
        "operational.session_initializer", {"session_id": str(session_id)}, session_id, "api", None, db
    )
    l1 = await rebuild_l1(session_id, db)

    await GoalLoop().start(session_id, db)

    return SessionOpenResponse(session_id=session_id, l1_summary=l1)


@router.post("/{session_id}/input", response_model=AgentResponse)
async def submit_input(
    session_id: UUID, body: InputRequest, db: AsyncSession = Depends(get_db)
) -> AgentResponse:
    await _get_active_session(session_id, db)
    loop_run_id = await _get_active_loop_run_id(session_id, db)

    response = await GoalLoop().execute_turn(loop_run_id, body.user_input, session_id, db)

    for approval in response.pending_approvals:
        db.add(
            PendingEscalation(
                escalation_type=EscalationType.CLARIFICATION,
                priority_class=PriorityClass.P1,
                content={"title": "Action awaiting approval", "description": f"{approval.action} on {approval.target}"},
                session_id=session_id,
                status=EscalationStatus.PENDING,
            )
        )
    if response.pending_approvals:
        await db.commit()

    return response


@router.post("/{session_id}/approve")
async def approve_action(
    session_id: UUID, body: ApproveRequestBody, db: AsyncSession = Depends(get_db)
) -> dict:
    await _get_active_session(session_id, db)

    action_log_row = (
        await db.execute(select(ActionLog).where(ActionLog.id == body.action_log_id))
    ).scalar_one_or_none()
    if action_log_row is None:
        raise HTTPException(status_code=404, detail="action_log_id not found")

    # RISK-AU01: cross-session approval forgery guard.
    if action_log_row.session_id != session_id:
        raise HTTPException(status_code=403, detail="action_log_id does not belong to this session")

    if action_log_row.user_confirmed:
        # This branch is realistically unreachable in practice — see
        # the fix below — but kept as a defensive check in case some
        # future write path ever does set user_confirmed=True directly
        # on the original row.
        raise HTTPException(status_code=409, detail="This action has already been approved")

    # Real bug, found only by testing the duplicate-approval path
    # specifically: action_log is append-only (no UPDATE at all, per
    # migration 0003's REVOKE), so approving an action can only ever
    # INSERT a new CONFIRM row — it can never set
    # action_log_row.user_confirmed=True on the *original* row being
    # approved. That means the check above never actually catches a
    # duplicate; every /approve call for the same action_log_id would
    # have silently succeeded again. Fixed by giving each CONFIRM row
    # a structured, queryable input_summary that names exactly which
    # action_log_id it approves, and checking for a prior one before
    # inserting.
    approval_marker = f"approve:{body.action_log_id}"
    existing_confirmation = (
        await db.execute(
            select(ActionLog).where(
                ActionLog.action_type == ActionType.CONFIRM,
                ActionLog.input_summary == approval_marker,
            )
        )
    ).scalar_one_or_none()
    if existing_confirmation is not None:
        raise HTTPException(status_code=409, detail="This action has already been approved")

    if not body.approved:
        await db.execute(
            update(PendingEscalation)
            .where(PendingEscalation.session_id == session_id, PendingEscalation.status == EscalationStatus.PENDING)
            .values(status=EscalationStatus.RESOLVED, response={"approved": False})
        )
        await db.commit()
        return {"status": "rejected"}

    # INV-06: this is the ONLY place in the codebase where
    # user_confirmed=True is set.
    db.add(
        ActionLog(
            session_id=session_id,
            agent=AgentName.MASTER,
            action_type=ActionType.CONFIRM,
            user_confirmed=True,
            input_summary=approval_marker[:500],
        )
    )
    await db.commit()

    gateway_result = await invoke_skill(
        "operational.action_gateway",
        {
            "action_type": action_log_row.action_type.value,
            "target": (action_log_row.input_summary or "").split("\u2192")[-1].strip(),
            "payload": {},
            "authority_level": 4,
            "session_id": str(session_id),
            "requesting_agent": "master",
        },
        session_id,
        "api",
        None,
        db,
    )
    return gateway_result.output


@router.post("/{session_id}/close")
async def close_session(session_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    await _get_active_session(session_id, db)
    loop_run_id = await _get_active_loop_run_id(session_id, db)

    await GoalLoop().complete(loop_run_id, LoopStatus.COMPLETED, db)

    await db.execute(
        update(Session).where(Session.id == session_id).values(status=SessionStatus.CLOSED, ended_at=func.now())
    )
    await db.commit()

    l1 = await rebuild_l1(session_id, db)
    await save_l1_snapshot(session_id, l1, db)
    await db.commit()

    # Session summary LLM call (temp=0.3). No dedicated prompt file
    # exists for this in Section 20's 10 files — flagged; a minimal
    # inline prompt is used instead of inventing an 11th prompts/*.txt
    # file the spec never asked for.
    summary_text = await _generate_session_summary(l1)
    await db.execute(update(Session).where(Session.id == session_id).values(summary=summary_text))
    await db.commit()

    nodes_written_total = (
        await db.execute(select(func.count()).select_from(Node).where(Node.session_id == session_id))
    ).scalar_one()
    pending_items = (
        await db.execute(
            select(func.count())
            .select_from(PendingEscalation)
            .where(PendingEscalation.session_id == session_id, PendingEscalation.status == EscalationStatus.PENDING)
        )
    ).scalar_one()

    # Synthesis threshold check.
    last_synthesis = (
        await db.execute(
            select(SynthesisRun.completed_at)
            .where(SynthesisRun.completed_at.isnot(None))
            .order_by(SynthesisRun.completed_at.desc())
            .limit(1)
        )
    ).first()
    since = last_synthesis[0] if last_synthesis else None
    node_count_query = select(func.count()).select_from(Node)
    if since is not None:
        node_count_query = node_count_query.where(Node.created_at > since)
    nodes_since_last = (await db.execute(node_count_query)).scalar_one()
    if nodes_since_last >= settings.synthesis_threshold_nodes:
        await run_synthesis("threshold", db)

    return {
        "summary": summary_text,
        "nodes_written_total": nodes_written_total,
        "pending_items": pending_items,
    }


async def _generate_session_summary(l1: L1WorkingMemory) -> str:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=256,
            temperature=0.3,
            system="Summarize this session's working memory state in 2-3 plain sentences for Blake.",
            messages=[{"role": "user", "content": json.dumps(l1.model_dump(mode="json"))}],
        )
        return "".join(block.text for block in response.content if block.type == "text")
    except Exception:
        logger.exception("Session summary LLM call failed; falling back to a generic summary")
        return "Session closed. Summary unavailable."
