"""
aether.schemas.gateway
========================

Pydantic v2 schemas for the Action Gateway stub and the ``/approve``
endpoint (Phase-0 Prompt Section 6, ``schemas/gateway.py``).

Phase-0's Action Gateway is a [STUB]: it validates, logs, and returns
``mock_executed`` — it never makes a real external call (Section 1.3).
These schemas are already shaped for Phase-1's live gateway so the
contract doesn't need to change later, only the implementation behind
``status == 'mock_executed'``.
"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel


class GatewayResult(BaseModel):
    """Result of an Action Gateway invocation (real or, in Phase-0, mocked)."""

    status: Literal["mock_executed", "blocked", "error"]
    mock_response: Optional[dict] = None
    reason: Optional[str] = None
    log_id: Optional[UUID] = None


class ApproveRequest(BaseModel):
    """Request body for ``POST /approve``."""

    approved: bool
    action_log_id: UUID
