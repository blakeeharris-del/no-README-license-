"""
aether.database
=================

Async SQLAlchemy engine, session factory, and the ``get_db()`` FastAPI
dependency (Phase-0 Prompt Section 2 / Step 4).

Note on Section 3's build ordering: this file is Step 4, and it
references ``aether.config.settings``, which is not implemented until
Step 7. That's fine as *source* — Python does not resolve the import
until this module is actually run, and no test in Section 22/23
imports ``database.py`` before ``config.py`` exists. The file is
written now, per the literal sequence in Section 3; it will not
successfully run until Step 7 is complete.

Per CLAUDE.md's layering rule ("memory/ imports from models/ only"),
``database.py`` itself is infrastructure shared by ``models`` and
``memory`` — it is not one of the four named layers (models, memory,
skills, agents) and sits underneath all of them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aether.config import settings
from aether.models.base import Base

logger = logging.getLogger("aether.database")

# ``pool_pre_ping``: cheap liveness check before handing out a pooled
# connection — protects against stale connections after Postgres
# restarts, at negligible latency cost. ``echo`` is tied to LOG_LEVEL
# rather than hardcoded, per coding convention #9 ("all config via
# settings object").
engine = create_async_engine(
    settings.database_url,
    echo=(settings.log_level.upper() == "DEBUG"),
    pool_pre_ping=True,
)

# ``expire_on_commit=False``: Phase-0 code frequently reads attributes
# off ORM objects (e.g. to build a Pydantic schema via
# ``from_attributes``) immediately after a commit within the same
# request; the default `True` would force a re-fetch on every such
# access.
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency yielding a single request-scoped ``AsyncSession``.

    On an unhandled exception within the request, the session is
    rolled back before being closed — callers that already committed
    what they needed to commit are unaffected; callers mid-transaction
    do not leak a half-written state.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception as exc:
            # FastAPI's HTTPException (404/403/409/etc.) is expected
            # control flow for a rejected request, not a failure of the
            # DB session itself — logging a full traceback for every
            # such response is noise, not signal. Anything else here
            # genuinely is unexpected and gets logged. Imported lazily,
            # inside the except block, so this low-level infrastructure
            # module doesn't take a module-level dependency on FastAPI.
            from fastapi import HTTPException

            if not isinstance(exc, HTTPException):
                logger.exception("Unhandled exception in DB session scope; rolling back")
            await session.rollback()
            raise
        finally:
            await session.close()


async def dispose_engine() -> None:
    """
    Dispose of the engine's connection pool.

    Not explicitly requested by Section 2/4, but required for a clean
    FastAPI ``lifespan`` shutdown (Step 21) — added here rather than
    deferred, since it is a one-line method on the same ``engine``
    object this module owns, and there is no other natural home for it.
    """
    await engine.dispose()
    logger.info("Database engine disposed")
