"""tests.api.test_session_endpoints — Phase-0 Prompt Section 23.

Uses the real ASGI app with the real lifespan (real pgvector check,
real watchdog startup) against the same test DB the other suites use.
Not run inside the db_session/savepoint fixture — the app's own
get_db() dependency opens its own real sessions per request, exactly
as it will in production, so each test cleans up the rows it created
explicitly rather than relying on an enclosing rollback.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from asgi_lifespan import LifespanManager

from aether.api.main import app
from aether.database import AsyncSessionLocal


def _intent_resp(action_type="query", pillars=None):
    m = MagicMock()
    m.content = [MagicMock(type="text", text=json.dumps({
        "action_type": action_type, "subject": "test", "implied_pillars": pillars or ["legal"],
        "urgency": "standard", "time_horizon": None, "entities": []
    }))]
    return m


def _reasoning_resp(text="Response.", **overrides):
    body = {"response": text, "write_proposals": [], "action_requests": [],
            "confidence": "explicit", "source_node_ids": [], "warnings": []}
    body.update(overrides)
    m = MagicMock()
    m.content = [MagicMock(type="text", text=json.dumps(body))]
    return m


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    # Cleanup: this suite creates real, committed rows (not inside the
    # savepoint fixture other suites use, since the app manages its own
    # sessions per-request). Real bug found writing this fixture: a
    # first attempt tried to DELETE using the app's own (restricted)
    # connection — which correctly has no DELETE privilege on any
    # table, the exact restriction this whole codebase enforces. Truncating
    # test data therefore requires the unrestricted migration-owner
    # connection instead, never the app's own.
    import os

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    owner_engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"], poolclass=NullPool)
    async with owner_engine.connect() as conn:
        await conn.execute(
            text(
                "TRUNCATE pending_escalations, loop_runs, action_log, nodes, sessions, "
                "node_pillars, node_links, skill_invocation_log CASCADE"
            )
        )
        await conn.commit()
    await owner_engine.dispose()


@pytest.mark.asyncio
async def test_session_open_creates_session_row_and_loop_run(client):
    r = await client.post("/session/start")
    assert r.status_code == 200
    body = r.json()
    assert "session_id" in body and "l1_summary" in body

    from sqlalchemy import select
    from aether.models.runtime import LoopRun

    async with AsyncSessionLocal() as db:
        loop_runs = (await db.execute(select(LoopRun).where(LoopRun.session_id == body["session_id"]))).scalars().all()
        assert len(loop_runs) == 1


@pytest.mark.asyncio
async def test_session_open_rebuilds_l1_for_new_user(client):
    r = await client.post("/session/start")
    l1 = r.json()["l1_summary"]
    assert l1["open_tasks"] == []
    assert l1["contradiction_count"] == 0


@pytest.mark.asyncio
async def test_session_input_returns_agent_response(client):
    r = await client.post("/session/start")
    session_id = r.json()["session_id"]

    with patch("aether.skills.cognitive.intent_parser.AsyncAnthropic") as mock_intent_cls, \
         patch("anthropic.AsyncAnthropic") as mock_master_cls:
        mock_intent_client = AsyncMock()
        mock_intent_client.messages.create = AsyncMock(return_value=_intent_resp())
        mock_intent_cls.return_value = mock_intent_client
        mock_master_client = AsyncMock()
        mock_master_client.messages.create = AsyncMock(return_value=_reasoning_resp("Real answer."))
        mock_master_cls.return_value = mock_master_client

        r2 = await client.post(f"/session/{session_id}/input", json={"user_input": "hi"})
        assert r2.status_code == 200
        assert r2.json()["text"] == "Real answer."


@pytest.mark.asyncio
async def test_session_input_on_closed_session_returns_404(client):
    r2 = await client.post("/session/00000000-0000-0000-0000-000000000000/input", json={"user_input": "x"})
    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_session_approve_sets_user_confirmed_true(client):
    r = await client.post("/session/start")
    session_id = r.json()["session_id"]

    from aether.models.logs import ActionLog
    from aether.models.enums import AgentName, ActionType
    async with AsyncSessionLocal() as db:
        entry = ActionLog(session_id=session_id, agent=AgentName.MASTER, action_type=ActionType.SURFACE, input_summary="write -> t")
        db.add(entry)
        await db.commit()
        action_log_id = str(entry.id)

    r2 = await client.post(f"/session/{session_id}/approve", json={"approved": True, "action_log_id": action_log_id})
    assert r2.status_code == 200

    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        confirm_rows = (await db.execute(
            select(ActionLog).where(ActionLog.action_type == ActionType.CONFIRM, ActionLog.user_confirmed == True)  # noqa: E712
        )).scalars().all()
        assert len(confirm_rows) >= 1


@pytest.mark.asyncio
async def test_session_approve_cross_session_action_returns_403(client):
    r1 = await client.post("/session/start")
    sid1 = r1.json()["session_id"]
    r2 = await client.post("/session/start")
    sid2 = r2.json()["session_id"]

    from aether.models.logs import ActionLog
    from aether.models.enums import AgentName, ActionType
    async with AsyncSessionLocal() as db:
        entry = ActionLog(session_id=sid1, agent=AgentName.MASTER, action_type=ActionType.SURFACE, input_summary="write -> t")
        db.add(entry)
        await db.commit()
        action_log_id = str(entry.id)

    r3 = await client.post(f"/session/{sid2}/approve", json={"approved": True, "action_log_id": action_log_id})
    assert r3.status_code == 403


@pytest.mark.asyncio
async def test_session_approve_rejected_resolves_escalation(client):
    r = await client.post("/session/start")
    session_id = r.json()["session_id"]

    from aether.models.logs import ActionLog
    from aether.models.runtime import PendingEscalation
    from aether.models.enums import AgentName, ActionType, EscalationType, PriorityClass, EscalationStatus
    async with AsyncSessionLocal() as db:
        entry = ActionLog(session_id=session_id, agent=AgentName.MASTER, action_type=ActionType.SURFACE, input_summary="write -> t")
        db.add(entry)
        db.add(PendingEscalation(escalation_type=EscalationType.CLARIFICATION, priority_class=PriorityClass.P1,
                                  content={"title": "x", "description": "y"}, session_id=session_id, status=EscalationStatus.PENDING))
        await db.commit()
        action_log_id = str(entry.id)

    r2 = await client.post(f"/session/{session_id}/approve", json={"approved": False, "action_log_id": action_log_id})
    assert r2.status_code == 200
    assert r2.json()["status"] == "rejected"

    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        esc = (await db.execute(select(PendingEscalation).where(PendingEscalation.session_id == session_id))).scalars().all()
        assert all(e.status == EscalationStatus.RESOLVED for e in esc)


@pytest.mark.asyncio
async def test_session_close_sets_ended_at_and_generates_summary(client):
    r = await client.post("/session/start")
    session_id = r.json()["session_id"]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_reasoning_resp())
        mock_cls.return_value = mock_client
        r2 = await client.post(f"/session/{session_id}/close")
        assert r2.status_code == 200
        assert "summary" in r2.json()

    from sqlalchemy import select
    from aether.models.sessions import Session
    from aether.models.enums import SessionStatus
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()
        assert row.status == SessionStatus.CLOSED
        assert row.ended_at is not None


@pytest.mark.asyncio
async def test_gateway_returns_mock_executed_in_t0(client):
    r = await client.post("/session/start")
    session_id = r.json()["session_id"]

    from aether.models.logs import ActionLog
    from aether.models.enums import AgentName, ActionType
    async with AsyncSessionLocal() as db:
        entry = ActionLog(session_id=session_id, agent=AgentName.MASTER, action_type=ActionType.SURFACE, input_summary="write -> t")
        db.add(entry)
        await db.commit()
        action_log_id = str(entry.id)

    r2 = await client.post(f"/session/{session_id}/approve", json={"approved": True, "action_log_id": action_log_id})
    assert r2.json()["status"] == "mock_executed"


@pytest.mark.asyncio
async def test_gateway_blocks_without_prior_approval(db_session, test_session_row):
    from aether.skills.operational.action_gateway import action_gateway_skill

    result = await action_gateway_skill(
        {"action_type": "write", "target": "x", "payload": {}, "authority_level": 4,
         "session_id": str(test_session_row.id), "requesting_agent": "master"},
        db_session,
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "no_approval"


@pytest.mark.asyncio
async def test_error_format_on_validation_failure_is_standard_json():
    from aether.api.main import standard_error_handler
    from aether.invariants.guards import NodeValidationError

    resp = await standard_error_handler(None, NodeValidationError("bad"))
    body = json.loads(resp.body)
    assert set(body.keys()) == {"error_code", "message", "details", "log_id"}
    assert resp.status_code == 400
