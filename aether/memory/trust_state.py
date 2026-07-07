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

import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aether.config import settings
from aether.models.enums import (
    ActionType,
    AgentName,
    EscalationType,
    LoopStatus,
    SessionStatus,
)
from aether.models.logs import ActionLog, DecisionRecord
from aether.models.runtime import LoopRun, PendingEscalation, SkillInvocationLog, SkillPerformance
from aether.models.sessions import Session

logger = logging.getLogger("aether.memory.trust_state")

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


# =====================================================================
# EC-35 — global trust-advancement ladder (T0 -> T1 -> T2 -> T3)
# =====================================================================
#
# Rulings this implements (Blake, recorded in HANDOFF_PHASE2.md):
#   - Trust is GLOBAL, one system-wide stage (§9.2 governs; §18 criterion
#     20's "two pillars" is the *evidence basis*, not a per-pillar stage).
#   - No stage advances automatically. AETHER SURFACES real-signal evidence
#     (read-only); Blake EXECUTES the advance. Surfacing and executing are
#     separate functions, separate roles.
#   - Evidence is real signals only — no computed "trust score".
#
# The ladder steps (no skipping). Reaching T3 walks all three.
_LADDER: dict[str, str] = {"T0": "T1", "T1": "T2", "T2": "T3"}

# Thresholds. Foundation defines concrete numbers only for the qualitative
# T0->T1 tier; T2/T3 numbers are a Phase-2 definition (a defensible analog
# to the existing T0->T1 rule) and are Blake-tunable — RAISABLE, not
# lowerable. Recorded as such in the ledger.
_T1_MIN_CLOSED_SESSIONS = 3      # existing T0->T1 bar (Phase-1), now surfaced
_T1_MIN_OK_INVOCATIONS = 5
_T2_MIN_CONFIRMED = 1            # first accurate L3 staging (§9.2 T2)
_T2_MIN_CLEAN_SESSIONS = 5
_T2_MIN_STREAK = 5
_T3_MIN_CONFIRMED_PER_PILLAR = 3   # §18 criterion-20 evidence basis...
_T3_MIN_PILLARS = 2                 # ...in >= 2 distinct pillars
_T3_MIN_CLEAN_SESSIONS = 10
_T3_MIN_STREAK = 10


class TrustAdvanceError(RuntimeError):
    """An execute_trust_advance precondition failed (wrong stage, invalid
    ladder step, or evidence not met). Never raised for a missing source —
    that is a ValueError (see execute_trust_advance)."""


@dataclass
class Signal:
    name: str
    value: int
    threshold: int
    met: bool


@dataclass
class AdvancementEvidence:
    target_stage: str
    from_stage: str
    met: bool
    signals: list[Signal] = field(default_factory=list)
    summary: str = ""


async def _dirty_session_ids(db: AsyncSession) -> set[UUID]:
    """Sessions with any invariant/incident signal — the complement of a
    'clean' session (approved definition): a forced_termination loop, a
    safety_alert/correction_exhaust escalation, or an authority_violation
    action_log row."""
    dirty: set[UUID] = set()
    for sid in (await db.execute(
        select(LoopRun.session_id).where(LoopRun.status == LoopStatus.FORCED_TERMINATION)
    )).scalars().all():
        if sid is not None:
            dirty.add(sid)
    for sid in (await db.execute(
        select(PendingEscalation.session_id).where(
            PendingEscalation.escalation_type.in_(
                (EscalationType.SAFETY_ALERT, EscalationType.CORRECTION_EXHAUST)
            )
        )
    )).scalars().all():
        if sid is not None:
            dirty.add(sid)
    for sid in (await db.execute(
        select(ActionLog.session_id).where(ActionLog.output_summary.like("authority_violation%"))
    )).scalars().all():
        if sid is not None:
            dirty.add(sid)
    return dirty


async def _clean_sessions(db: AsyncSession) -> tuple[int, int]:
    """(clean_closed_count, clean_streak). Streak = consecutive most-recent
    closed sessions that are clean."""
    dirty = await _dirty_session_ids(db)
    closed = (await db.execute(
        select(Session.id).where(
            Session.status == SessionStatus.CLOSED, Session.ended_at.isnot(None)
        ).order_by(Session.ended_at.desc())
    )).scalars().all()
    clean_count = sum(1 for sid in closed if sid not in dirty)
    streak = 0
    for sid in closed:  # most recent first
        if sid in dirty:
            break
        streak += 1
    return clean_count, streak


async def _confirmed_pillars(db: AsyncSession) -> dict[str, int]:
    """Blake-confirmed-correct decisions grouped by pillar — the audited L3
    accuracy signal (decision_journal, the EC-38 source)."""
    rows = (await db.execute(
        text(
            """
            SELECT p AS pillar, count(*) AS n
            FROM decision_journal, jsonb_array_elements_text(pillars) AS p
            WHERE confirmed_correct = true AND confirmed_by = 'blake'
            GROUP BY p
            """
        )
    )).all()
    return {r.pillar: int(r.n) for r in rows}


async def surface_advancement_evidence(target_stage: str, db: AsyncSession) -> AdvancementEvidence:
    """READ-ONLY. Return the real-signal audit record for advancing to
    ``target_stage``. Advances nothing, writes nothing — this is AETHER's
    surface role. Every number traces to real rows; no 'trust score'."""
    from_stage = await current_trust_stage(db)
    signals: list[Signal] = []

    if target_stage == "T1":
        closed = (await db.execute(
            select(func.count()).select_from(Session).where(Session.status == SessionStatus.CLOSED)
        )).scalar_one()
        ok_inv = (await db.execute(
            select(func.count()).select_from(SkillInvocationLog)
            .where(SkillInvocationLog.status == "ok")
        )).scalar_one()
        signals = [
            Signal("closed_sessions", closed, _T1_MIN_CLOSED_SESSIONS, closed >= _T1_MIN_CLOSED_SESSIONS),
            Signal("ok_skill_invocations", ok_inv, _T1_MIN_OK_INVOCATIONS, ok_inv >= _T1_MIN_OK_INVOCATIONS),
        ]
    elif target_stage == "T2":
        confirmed_total = sum((await _confirmed_pillars(db)).values())
        clean, streak = await _clean_sessions(db)
        signals = [
            Signal("confirmed_correct_decisions", confirmed_total, _T2_MIN_CONFIRMED, confirmed_total >= _T2_MIN_CONFIRMED),
            Signal("clean_closed_sessions", clean, _T2_MIN_CLEAN_SESSIONS, clean >= _T2_MIN_CLEAN_SESSIONS),
            Signal("zero_violation_streak", streak, _T2_MIN_STREAK, streak >= _T2_MIN_STREAK),
        ]
    elif target_stage == "T3":
        per_pillar = await _confirmed_pillars(db)
        pillars_meeting = sum(1 for n in per_pillar.values() if n >= _T3_MIN_CONFIRMED_PER_PILLAR)
        clean, streak = await _clean_sessions(db)
        degraded = (await db.execute(
            select(func.count()).select_from(SkillPerformance)
            .where(SkillPerformance.below_threshold.is_(True))
        )).scalar_one()
        signals = [
            Signal(f"pillars_with_>={_T3_MIN_CONFIRMED_PER_PILLAR}_confirmed", pillars_meeting, _T3_MIN_PILLARS, pillars_meeting >= _T3_MIN_PILLARS),
            Signal("clean_closed_sessions", clean, _T3_MIN_CLEAN_SESSIONS, clean >= _T3_MIN_CLEAN_SESSIONS),
            Signal("zero_violation_streak", streak, _T3_MIN_STREAK, streak >= _T3_MIN_STREAK),
            Signal("degraded_skills", int(degraded), 0, int(degraded) == 0),
        ]
    else:
        raise TrustAdvanceError(f"no evidence rule for target stage {target_stage!r}")

    met = all(s.met for s in signals)
    summary = "; ".join(f"{s.name}={s.value}/{s.threshold}" for s in signals)
    return AdvancementEvidence(target_stage=target_stage, from_stage=from_stage,
                               met=met, signals=signals, summary=summary)


async def execute_trust_advance(
    from_stage: str, to_stage: str, confirmed_by: str, session_id: UUID, db: AsyncSession
) -> str:
    """The ONLY path that advances the trust stage (Blake's execute role).

    Structurally requires a non-empty ``confirmed_by`` — an advance with no
    source is impossible (ValueError here AND the action_log CHECK
    ``ck_action_log_trust_marker_sourced`` in migration 0007, mirroring the
    decision_journal confirmation discipline). It is NOT auto-triggered by
    evidence passing: a caller (Blake) must invoke it. It still refuses to
    advance unless the evidence bar for ``to_stage`` is met and the step is
    a valid single rung on the ladder from the live current stage.
    """
    if not confirmed_by or not confirmed_by.strip():
        raise ValueError("execute_trust_advance requires a non-empty confirmed_by source")

    current = await current_trust_stage(db)
    if current != from_stage:
        raise TrustAdvanceError(f"from_stage {from_stage!r} != live current stage {current!r}")
    if _LADDER.get(from_stage) != to_stage:
        raise TrustAdvanceError(f"{from_stage!r}->{to_stage!r} is not a valid ladder step")

    evidence = await surface_advancement_evidence(to_stage, db)
    if not evidence.met:
        raise TrustAdvanceError(f"evidence for {to_stage} not met: {evidence.summary}")

    db.add(ActionLog(
        session_id=session_id,
        agent=AgentName.MASTER,
        action_type=ActionType.SURFACE,
        output_summary=(
            f"{TRUST_MARKER} {from_stage}->{to_stage}: confirmed_by={confirmed_by}; {evidence.summary}"
        )[:500],
    ))
    await db.flush()
    logger.info("Trust maturity advanced %s->%s (confirmed_by=%s)", from_stage, to_stage, confirmed_by)
    return to_stage
