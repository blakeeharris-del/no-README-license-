"""
aether.models.sessions
=======================

ORM model for the ``sessions`` table (Phase-0 Prompt Section 5.2).

A session is the unit of continuity between Blake and Aether (Foundation
P2 — Continuity). ``l1_snapshot`` holds the serialized L1WorkingMemory
(see ``aether/schemas/session.py`` and ``aether/memory/session_state.py``)
so that a session can be rebuilt exactly where it left off (INV-09:
System State Is Always Recoverable).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from aether.models.base import Base
from aether.models.enums import SessionStatus, pg_enum


class Session(Base):
    """TABLE: sessions"""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[SessionStatus] = mapped_column(
        pg_enum(SessionStatus, "session_status"),
        nullable=False,
        server_default=SessionStatus.ACTIVE.value,
    )
    # L1WorkingMemory, serialized. NULL until first rebuild/snapshot.
    l1_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Generated on session close (Shutdown Loop, out of scope for Phase-0
    # beyond the column existing).
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Session id={self.id} status={self.status} started_at={self.started_at}>"
