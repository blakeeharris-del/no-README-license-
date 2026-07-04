"""
tests.conftest
================

Shared fixtures (Phase-0 Prompt Section 23). No test touches a real
dev/production DB — ``db_session`` wraps each test in an outer
transaction on a dedicated connection and rolls it back afterward,
using SQLAlchemy's ``join_transaction_mode="create_savepoint"``
pattern: application code throughout this codebase calls
``session.commit()`` liberally (write_node, invoke_skill, etc.), and
under this pattern each such "commit" only releases a SAVEPOINT —
the outer transaction on the connection is never actually committed,
so a full rollback after the test undoes everything regardless of how
many times the code under test called commit().

Tests run against the *real* Postgres instance configured for this
environment (same DB Alembic migrations were verified against
throughout every checkpoint) — not an in-memory substitute — since
several invariants (native enum types, RLS/GRANT enforcement, fulltext
search, advisory locks) have no meaningful SQLite/mock equivalent.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://aether_app_role:apppassword@localhost:5432/aether")
os.environ.setdefault("MIGRATION_DATABASE_URL", "postgresql+asyncpg://aether:password@localhost:5432/aether")
os.environ.setdefault("AETHER_APP_DB_PASSWORD", "apppassword")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("AETHER_TRUST_STAGE", "T0")
os.environ.setdefault("SYNTHESIS_SCHEDULE_UTC", "02:00")
os.environ.setdefault("SYNTHESIS_THRESHOLD_NODES", "20")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("WATCHDOG_CHECK_INTERVAL_MS", "1000")
os.environ.setdefault("DEFAULT_SKILL_TIMEOUT_MS", "30000")
os.environ.setdefault("LLM_MAX_RETRIES", "3")
os.environ.setdefault("LLM_CONTEXT_TOKEN_BUDGET", "8000")

from aether.models.enums import (  # noqa: E402
    ConfidenceLevel,
    CreatedByAgent,
    NodeSource,
    NodeStatus,
    NodeType,
    PillarName,
    SessionStatus,
)
from aether.models.nodes import Node, NodePillar  # noqa: E402
from aether.models.sessions import Session  # noqa: E402

_TEST_DB_URL = os.environ["DATABASE_URL"]


@pytest.fixture
def settings_override(monkeypatch):
    """aether_trust_stage='T0', anthropic_api_key='test-key' for all tests."""
    from aether.config import settings

    monkeypatch.setattr(settings, "aether_trust_stage", "T0")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    return settings


@pytest_asyncio.fixture
async def db_session():
    """Transactional AsyncSession; rolled back after every test.

    The engine is created *inside* this fixture, not at module level —
    SQLAlchemy's async engine (and the asyncpg connections it opens)
    binds to whichever asyncio event loop is running at connection
    time, and a module-level engine created at import time (before any
    test's event loop exists) causes "attached to a different loop"
    errors the moment two tests run under different loops. NullPool
    avoids any pooled-connection reuse across the fixture's own
    teardown/setup boundary for the same reason.
    """
    test_engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    try:
        async with test_engine.connect() as connection:
            await connection.begin()
            session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
                await connection.rollback()
    finally:
        await test_engine.dispose()


@pytest_asyncio.fixture
async def test_session_row(db_session: AsyncSession) -> Session:
    """An active Session row for use in tests."""
    session_row = Session(status=SessionStatus.ACTIVE)
    db_session.add(session_row)
    await db_session.commit()
    return session_row


@pytest_asyncio.fixture
async def sample_nodes(db_session: AsyncSession, test_session_row: Session) -> list[Node]:
    """Nodes across all pillars with varied confidence, status, type."""
    specs = [
        (PillarName.LEGAL, NodeType.FACT, ConfidenceLevel.EXPLICIT, NodeStatus.ACTIVE),
        (PillarName.PERSONAL_FINANCE, NodeType.TASK, ConfidenceLevel.INFERRED, NodeStatus.ACTIVE),
        (PillarName.CAREER, NodeType.GOAL, ConfidenceLevel.SPECULATIVE, NodeStatus.PENDING_REVIEW),
        (PillarName.BUSINESS, NodeType.EVENT, ConfidenceLevel.INFERRED, NodeStatus.FLAGGED),
        (PillarName.HEALTH, NodeType.SIGNAL, ConfidenceLevel.SPECULATIVE, NodeStatus.ACTIVE),
        (PillarName.RELATIONSHIPS, NodeType.REFLECTION, ConfidenceLevel.EXPLICIT, NodeStatus.ARCHIVED),
    ]
    nodes = []
    for pillar, node_type, confidence, status in specs:
        node = Node(
            type=node_type,
            title=f"Sample {pillar.value} node",
            content=f"Sample content for {pillar.value}",
            source=NodeSource.USER_EXPLICIT if confidence == ConfidenceLevel.EXPLICIT else NodeSource.AGENT_WRITE,
            confidence=confidence,
            status=status,
            created_by=CreatedByAgent.USER if confidence == ConfidenceLevel.EXPLICIT else CreatedByAgent.MASTER_AGENT,
            session_id=test_session_row.id,
            metadata_={},
        )
        db_session.add(node)
        await db_session.flush()
        db_session.add(NodePillar(node_id=node.id, pillar=pillar, is_primary=True, assigned_by=node.created_by))
        nodes.append(node)
    await db_session.commit()
    return nodes


@pytest.fixture
def mock_llm_client(monkeypatch):
    """
    Patches every module-level Anthropic client constructor this
    codebase uses to return deterministic responses. Returns a small
    controller object tests can configure per-call via
    ``set_responses([...])``.
    """
    from unittest.mock import AsyncMock, MagicMock

    state = {"responses": []}

    def _make_text_response(text: str):
        m = MagicMock()
        m.content = [MagicMock(type="text", text=text)]
        return m

    async def _create(*args, **kwargs):
        if state["responses"]:
            return state["responses"].pop(0)
        return _make_text_response('{"response": "default mock response", '
                                    '"write_proposals": [], "action_requests": [], '
                                    '"confidence": "explicit", "source_node_ids": [], "warnings": []}')

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=_create)

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", MagicMock(return_value=mock_client))
    monkeypatch.setattr(
        "aether.skills.cognitive.intent_parser.AsyncAnthropic", MagicMock(return_value=mock_client)
    )
    monkeypatch.setattr(
        "aether.skills.cognitive.contradiction_detector.AsyncAnthropic", MagicMock(return_value=mock_client)
    )

    class Controller:
        def set_responses(self, texts: list[str]) -> None:
            state["responses"] = [_make_text_response(t) for t in texts]

    return Controller()
