"""
aether.api.main
==================

FastAPI app with lifespan (Phase-0 Prompt Section 19). 4 endpoints
only. No authentication in Phase-0.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from aether.database import AsyncSessionLocal, dispose_engine, engine
from aether.invariants.guards import (
    AuthorityViolationError,
    ConfidenceViolationError,
    ContextPacketValidationError,
    LLMUnavailableError,
    NodeValidationError,
)
from aether.loops.watchdog import LoopWatchdog

logger = logging.getLogger("aether.api.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- STARTUP -------------------------------------------------------------
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT * FROM pg_extension WHERE extname='vector'"))
        if result.first() is None:
            logger.critical("pgvector extension is not installed; refusing to start")
            raise SystemExit(1)

    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                UPDATE skill_invocation_log
                SET status = 'timeout'
                WHERE status = 'running' AND timestamp < now() - interval '60 seconds'
                """
            )
        )
        await db.commit()

    await LoopWatchdog.start(lambda: AsyncSessionLocal())
    logger.info("Aether Phase-0 started. Watchdog running.")

    yield

    # ---- SHUTDOWN ------------------------------------------------------------
    await LoopWatchdog.stop()
    await dispose_engine()


app = FastAPI(title="Aether", version="0.1.0-phase0", lifespan=lifespan)

# error_code -> HTTP status, per Section 19's "STANDARD ERROR FORMAT" table.
_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    NodeValidationError: 400,
    ContextPacketValidationError: 400,
    AuthorityViolationError: 403,
    ConfidenceViolationError: 403,
    LLMUnavailableError: 503,
}


@app.exception_handler(Exception)
async def standard_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Standard error envelope for every unhandled exception (Section 19):
    ``{error_code, message, details, log_id}``. ``log_id`` is always
    null here — none of the exception types this handler catches carry
    one (only ``SkillResult``/``GatewayResult`` do, and those are
    handled inline in their own routes, not via this fallback path).
    """
    status_code = 500
    for exc_type, mapped_status in _EXCEPTION_STATUS_MAP.items():
        if isinstance(exc, exc_type):
            status_code = mapped_status
            break

    if status_code == 500:
        logger.exception("Unhandled internal error", exc_info=exc)
        message = "An internal error occurred."
    else:
        message = str(exc)

    return JSONResponse(
        status_code=status_code,
        content={
            "error_code": type(exc).__name__,
            "message": message,
            "details": None,
            "log_id": None,
        },
    )


from aether.api.routes import session  # noqa: E402  (registered after `app` exists)

app.include_router(session.router)
