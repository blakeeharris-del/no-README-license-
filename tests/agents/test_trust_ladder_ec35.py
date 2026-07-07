"""
tests.agents.test_trust_ladder_ec35 — EC-35.

The global trust-advancement ladder (T0->T1->T2->T3), built to Blake's
rulings: trust is one system-wide stage; NO stage advances automatically
(T0->T1 included); AETHER surfaces real-signal evidence (read-only), Blake
executes the advance; evidence is real signals only (no "trust score").

Forced against real Postgres: each gate proven, the surface!=execute
separation proven, the empty-source rejection proven (code + DB CHECK),
and the deliverable — driven to T3 with confirmed decisions in >=2 distinct
pillars, and a confirm-gated (T>=T3) action that was blocked at T2 now
proceeds at T3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aether.agents.trust import evaluate_and_advance
from aether.memory.trust_state import (
    TrustAdvanceError,
    current_trust_stage,
    execute_trust_advance,
    surface_advancement_evidence,
)
from aether.models.enums import ActionType, AgentName, SessionStatus
from aether.models.logs import ActionLog, DecisionRecord
from aether.models.runtime import SkillInvocationLog
from aether.models.sessions import Session


async def _seed_full_t3_evidence(db, active_session_id):
    """Seed enough real signals to meet the T3 bar (and, being a superset,
    the T1/T2 bars): 15 very-recent clean closed sessions, 6 Blake-confirmed
    decisions across two pillars, and ok skill invocations."""
    now = datetime.now(timezone.utc)
    for i in range(1, 16):  # most-recent-first streak of 15 clean closed sessions
        db.add(Session(status=SessionStatus.CLOSED, ended_at=now - timedelta(seconds=i)))
    for pillar in ("legal", "career"):        # two distinct pillars, 3 confirmed each
        for j in range(3):
            db.add(DecisionRecord(
                session_id=active_session_id, proposed_action=f"{pillar} decision {j}",
                pillars=[pillar], sense_summary="s", analysis="a", challenge="c",
                recommendation="No recommendation is offered by default.",
                deferred=True, approval_required=True,
                confirmed_correct=True, confirmed_by="blake", confirmed_at=now,
            ))
    for k in range(6):
        db.add(SkillInvocationLog(
            skill_name="cognitive.signal_scorer", invoked_by="test",
            session_id=active_session_id, inputs_hash=f"h{k}", status="ok",
        ))
    await db.flush()


# ---------------------------------------------------------------------------
# surface is read-only; below the bar is "not met"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_surface_is_readonly_and_below_bar_not_met(db_session, test_session_row):
    # No confirmed decisions seeded -> T3 pillar signal is 0 -> not met.
    ev = await surface_advancement_evidence("T3", db_session)
    assert ev.met is False
    assert any(s.name.startswith("pillars_with_") and not s.met for s in ev.signals)
    # Surfacing advanced nothing.
    assert await current_trust_stage(db_session) == "T0"


# ---------------------------------------------------------------------------
# surface != execute (the core of ruling #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_met_but_no_advance_until_execute(db_session, test_session_row):
    await _seed_full_t3_evidence(db_session, test_session_row.id)

    ev = await surface_advancement_evidence("T1", db_session)
    assert ev.met is True                                   # bar is met
    assert await current_trust_stage(db_session) == "T0"    # ...but not advanced

    # AETHER's role (no source) surfaces only — still no advance.
    assert await evaluate_and_advance(db_session, test_session_row.id) == "T0"
    assert await current_trust_stage(db_session) == "T0"

    # Blake's role (explicit source) executes the advance.
    assert await execute_trust_advance("T0", "T1", "blake", test_session_row.id, db_session) == "T1"
    assert await current_trust_stage(db_session) == "T1"


# ---------------------------------------------------------------------------
# an advance with no source is impossible (code + DB CHECK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_source_rejected_in_code_and_db(db_session, test_session_row):
    await _seed_full_t3_evidence(db_session, test_session_row.id)

    with pytest.raises(ValueError):
        await execute_trust_advance("T0", "T1", "", test_session_row.id, db_session)
    with pytest.raises(ValueError):
        await execute_trust_advance("T0", "T1", "   ", test_session_row.id, db_session)

    # DB CHECK (0007): a trust_maturity marker without confirmed_by= is rejected.
    db_session.add(ActionLog(
        session_id=test_session_row.id, agent=AgentName.MASTER, action_type=ActionType.SURFACE,
        output_summary="trust_maturity T0->T1: earned with no source",
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# invalid ladder steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_skip_or_misstep_the_ladder(db_session, test_session_row):
    await _seed_full_t3_evidence(db_session, test_session_row.id)
    # Cannot skip T0->T2, and cannot advance from a non-current from_stage.
    with pytest.raises(TrustAdvanceError):
        await execute_trust_advance("T0", "T2", "blake", test_session_row.id, db_session)
    with pytest.raises(TrustAdvanceError):
        await execute_trust_advance("T1", "T2", "blake", test_session_row.id, db_session)  # current is T0


# ---------------------------------------------------------------------------
# EC-35 deliverable: walk to T3, gate blocked at T2 now proceeds at T3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ec35_full_ladder_to_t3_unblocks_confirm_gate(db_session, test_session_row):
    from aether.skills.safety.authority_checker import check_authority_skill

    sid = test_session_row.id

    # Below the bar first: T3 evidence not met before seeding.
    assert (await surface_advancement_evidence("T3", db_session)).met is False

    await _seed_full_t3_evidence(db_session, sid)

    # Walk the net-new ladder, each step Blake-signed.
    assert await execute_trust_advance("T0", "T1", "blake", sid, db_session) == "T1"
    assert await execute_trust_advance("T1", "T2", "blake", sid, db_session) == "T2"

    # At T2, a confirm (L3) action is gated off (needs T>=T3) — the gate
    # reads the live stage (trust-wiring 5e686de), no new wiring.
    at_t2 = await check_authority_skill(
        {"agent": "master", "action_type": "confirm", "level": 3, "session_id": str(sid)}, db_session
    )
    assert at_t2["authorized"] is False

    # T3 evidence shows confirmed accuracy in >=2 distinct pillars (criterion 20 basis).
    t3_ev = await surface_advancement_evidence("T3", db_session)
    assert t3_ev.met is True
    pillar_signal = next(s for s in t3_ev.signals if s.name.startswith("pillars_with_"))
    assert pillar_signal.value >= 2

    # Blake executes the final advance.
    assert await execute_trust_advance("T2", "T3", "blake", sid, db_session) == "T3"
    assert await current_trust_stage(db_session) == "T3"

    # The same confirm action that was blocked at T2 now proceeds at T3.
    at_t3 = await check_authority_skill(
        {"agent": "master", "action_type": "confirm", "level": 3, "session_id": str(sid)}, db_session
    )
    assert at_t3["authorized"] is True

    # The advance marker is sourced.
    marker = (await db_session.execute(
        select(ActionLog).where(ActionLog.output_summary.like("trust_maturity T2->T3%"))
    )).scalars().first()
    assert marker is not None and "confirmed_by=blake" in marker.output_summary
