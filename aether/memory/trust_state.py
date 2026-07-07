"""
aether.memory.trust_state
============================

The canonical *reader* for the live (dynamic) trust maturity stage
(Foundation §9.2). It lives in the ``memory`` layer — not ``agents`` —
so that both the higher ``agents`` layer (``agents.trust``) and the
``skills`` layer's authority gates (``operational.action_gateway``,
``safety.authority_checker``) can consume it without violating the
``agents -> skills -> memory -> models`` layering contract (setup.cfg).
Skills may import from ``memory``; they may not import from ``agents``,
which is where this logic used to live.

Trust stages are *earned on evidence* and recorded as ``action_log``
rows tagged ``trust_maturity`` (Phase-1 stored the stage in the audit
log rather than a dedicated table, HANDOFF_PHASE1). This module derives
the live stage from the most recent such row, falling back to the
static config default when no transition has been logged yet.

Phase-2 (EC-35): this reader is now wired into the authority-checking
gates, closing the Phase-1 deferral where the dynamic stage was
logged-but-inert and the gates read static config.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.config import settings
from aether.models.logs import ActionLog

# The action_log.output_summary marker written by a trust transition,
# e.g. "trust_maturity T0->T1: <reason>". Kept in sync with the writer
# in aether.agents.trust.
TRUST_MARKER = "trust_maturity"

_STAGE_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}


def _destination_stage(summary: str | None) -> str | None:
    """Parse the destination stage from a 'trust_maturity T0->T1: ...' row."""
    if not summary or "->" not in summary:
        return None
    try:
        stage = summary.split("->", 1)[1].split(":", 1)[0].strip()
    except IndexError:
        return None
    return stage if stage in _STAGE_RANK else None


async def current_trust_stage(db: AsyncSession) -> str:
    """Live trust stage: the most recent logged transition, else config default.

    Ordering bug found by the EC-35 wiring test, fixed at this site:
    ``action_log.timestamp`` defaults to Postgres ``now()``, which
    returns the *transaction* start time — so two transitions committed
    within one transaction share an identical timestamp. The previous
    ``ORDER BY timestamp DESC LIMIT 1`` could then return either row
    non-deterministically (the test advanced T1 then T3 in one
    transaction and got back T1). In production each advance is its own
    transaction with a distinct ``now()``, so this never bit before —
    but reaching T3 in Phase 2 means several advances, and the reader
    must not depend on them never sharing a timestamp.

    Fix: order by ``(timestamp, destination-stage rank)`` and take the
    max. This is correct *because* Foundation §9.2 defines trust maturity
    as monotonic-ascending only — it is "earned through demonstrated
    performance" and "advances on evidence" (§7), and the Foundation
    defines no mechanism by which the trust *stage* drops (standing
    authorities held within a stage are renewable/revocable; the stage
    itself is not). So among rows sharing the latest timestamp the
    highest-ranked stage IS the genuinely-latest transition, and "highest
    rank wins" cannot fail open. Trust transitions are rare, so scanning
    them in Python (vs. a SQL LIMIT 1) is cheap and fully deterministic.

    ON RECORD — assumption to re-visit: if a future Foundation version
    introduces trust *de-escalation* (a stage that can drop), this
    rank-based tie-break becomes wrong — a demotion sharing a timestamp
    with another transition would resolve upward, granting more authority
    than earned. At that point switch the tie-break to true insertion
    order (a monotonic sequence column on action_log), so the genuinely
    latest transition wins regardless of direction.
    """
    rows = (
        await db.execute(
            select(ActionLog.output_summary, ActionLog.timestamp).where(
                ActionLog.output_summary.like(f"{TRUST_MARKER}%")
            )
        )
    ).all()

    best_key: tuple | None = None
    best_stage: str | None = None
    for summary, ts in rows:
        stage = _destination_stage(summary)
        if stage is None:
            continue
        key = (ts, _STAGE_RANK[stage])
        if best_key is None or key > best_key:
            best_key = key
            best_stage = stage

    return best_stage if best_stage is not None else settings.aether_trust_stage
