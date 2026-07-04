"""
aether.memory.synthesis
=========================

``run_synthesis()`` — Phase-0 Prompt Section 2/3 call this a "stub"
(repo-structure comment: "run_synthesis() stub"; build-order Step 11:
"run_synthesis() stub"), but no section of the Phase-0 Prompt gives it
a numbered-steps spec the way ``write_node()`` (Section 10) or
``rebuild_l1()`` (Section 12) get one. The actual synthesis engine
(``cognitive.synthesis_engine``, ``orchestrator.synthesis_coordinator``)
is Phase-1 work per the Implementation Plan's skill roster — Phase-0
has no skill capable of producing L3 candidate nodes at all.

What Phase-0 CAN meaningfully do, and what this stub implements:
  - Acquire the same Postgres advisory lock a real synthesis run would
    use, so Phase-1's concurrency behavior (Missing Specs GAP-12: "When
    pg_try_advisory_lock fails, SKIP the triggered run... log WARNING")
    is already correct and tested before there's anything real to
    protect with it.
  - Create and immediately complete a ``synthesis_runs`` row with
    zero nodes processed/written — an honest record that a synthesis
    cycle was triggered and did nothing, rather than silently
    no-op-ing with no trace at all.

This interpretation is a judgment call, not a literal spec
implementation, since none exists for Phase-0 to follow exactly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.logs import SynthesisRun

logger = logging.getLogger("aether.memory.synthesis")

# Arbitrary but fixed advisory lock key. Any single 64-bit (or two
# 32-bit) integer works; this value has no meaning beyond being
# consistently reused so concurrent callers actually contend on it.
_SYNTHESIS_ADVISORY_LOCK_KEY = 847_291_003


async def run_synthesis(triggered_by: str, db: AsyncSession) -> SynthesisRun | None:
    """
    Attempt a synthesis cycle.

    Returns the created ``SynthesisRun`` row, or ``None`` if a
    concurrent run already holds the advisory lock (per GAP-12: skip,
    don't queue — the next scheduled/threshold/manual trigger will
    retry naturally).
    """
    lock_acquired = (
        await db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _SYNTHESIS_ADVISORY_LOCK_KEY})
    ).scalar_one()

    if not lock_acquired:
        logger.warning("Synthesis skipped — lock held by concurrent run.")
        return None

    try:
        run = SynthesisRun(
            triggered_by=triggered_by,
            nodes_processed=0,
            nodes_written=0,
            diff_report={
                "note": (
                    "Phase-0 stub: no synthesis engine skill exists yet. "
                    "This run recorded the trigger and did no analytical work."
                )
            },
        )
        db.add(run)
        await db.flush()
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info("Synthesis stub run recorded", extra={"run_id": str(run.id)})
        return run
    finally:
        await db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _SYNTHESIS_ADVISORY_LOCK_KEY})
