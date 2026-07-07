"""
tests.skills.test_standing_authority_ec36 — EC-36 (standing L4 authority).

The highest-risk path in the system: a false positive here = an
unauthorized external action. So the gateway bypass is tested
adversarially — every failure mode must FALL THROUGH to per-action
approval (never auto-approve). Only the exact conjunction (active grant
covering (action_name, pillar) AND live T>=T3 AND within bounds AND before
renewal) skips approval.

Rulings built to: propose-then-approve (a grant exists only as a
Blake-approved row, never inferred from repeated approvals); per-pillar
scoped; reversible-only (Blake's per-grant judgment, CHECK-enforced);
renewal dates; revoke != delete (INV-02).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from aether.memory.standing_authority import (
    grant_standing_authority,
    propose_standing_authorities,
)
from aether.models.enums import ActionType, AgentName, PillarName
from aether.models.logs import ActionLog
from aether.models.runtime import StandingAuthority
from aether.skills.operational.action_gateway import action_gateway_skill

_FUTURE = datetime(2027, 1, 1, tzinfo=timezone.utc)
_PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)


async def _mark_trust(db, session_id, transition: str):
    """Set the live trust stage by logging a (sourced) trust_maturity
    marker, e.g. 'T2->T3' -> current_trust_stage == 'T3'."""
    db.add(ActionLog(
        session_id=session_id, agent=AgentName.MASTER, action_type=ActionType.SURFACE,
        output_summary=f"trust_maturity {transition}: confirmed_by=blake; test",
    ))
    await db.commit()


async def _grant(db, pillar, action_name, *, bounds=None, renewal=_FUTURE, granted_by="blake"):
    return await grant_standing_authority(
        pillar=pillar, action_type=action_name, bounds=bounds or {},
        rationale="reversible, routine, bounded — test rule", granted_by=granted_by,
        renewal_date=renewal, db=db,
    )


async def _gateway(db, session_id, *, action_name, pillar, payload=None,
                   action_type="write", authority_level=4):
    return await action_gateway_skill(
        {
            "action_type": action_type, "target": "t", "payload": payload or {},
            "authority_level": authority_level, "session_id": str(session_id),
            "requesting_agent": "master", "action_name": action_name, "pillar": pillar,
        },
        db,
    )


# ---------------------------------------------------------------------------
# The positive case: only the exact conjunction skips per-action approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positive_valid_grant_at_t3_auto_approves_and_logs(db_session, test_session_row):
    sid = test_session_row.id
    await _mark_trust(db_session, sid, "T2->T3")
    grant = await _grant(db_session, PillarName.PERSONAL_FINANCE, "categorize_transaction")
    await db_session.commit()

    r = await _gateway(db_session, sid, action_name="categorize_transaction",
                       pillar="personal_finance")
    # Proceeded WITHOUT any per-action approval record.
    assert r["status"] == "mock_executed"
    # ...and it was logged (INV-05 permanent per-execution record under the grant).
    logged = (await db_session.execute(
        select(ActionLog).where(ActionLog.output_summary.like("executed under standing_authority%"))
    )).scalars().all()
    assert any(str(grant.id) in (a.output_summary or "") for a in logged)


# ---------------------------------------------------------------------------
# Six adversarial fall-throughs: each must NOT auto-approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lapsed_grant_falls_through(db_session, test_session_row):
    sid = test_session_row.id
    await _mark_trust(db_session, sid, "T2->T3")
    await _grant(db_session, PillarName.PERSONAL_FINANCE, "categorize_transaction", renewal=_PAST)
    await db_session.commit()
    r = await _gateway(db_session, sid, action_name="categorize_transaction", pillar="personal_finance")
    assert r["status"] == "blocked" and r["reason"] == "no_approval"


@pytest.mark.asyncio
async def test_revoked_grant_falls_through(db_session, test_session_row):
    sid = test_session_row.id
    await _mark_trust(db_session, sid, "T2->T3")
    g = await _grant(db_session, PillarName.PERSONAL_FINANCE, "categorize_transaction")
    g.status = "revoked"           # revoke != delete (INV-02)
    await db_session.commit()
    r = await _gateway(db_session, sid, action_name="categorize_transaction", pillar="personal_finance")
    assert r["status"] == "blocked" and r["reason"] == "no_approval"


@pytest.mark.asyncio
async def test_wrong_pillar_falls_through(db_session, test_session_row):
    sid = test_session_row.id
    await _mark_trust(db_session, sid, "T2->T3")
    await _grant(db_session, PillarName.PERSONAL_FINANCE, "categorize_transaction")
    await db_session.commit()
    # request is for a DIFFERENT pillar than the grant
    r = await _gateway(db_session, sid, action_name="categorize_transaction", pillar="legal")
    assert r["status"] == "blocked" and r["reason"] == "no_approval"


@pytest.mark.asyncio
async def test_wrong_action_falls_through(db_session, test_session_row):
    sid = test_session_row.id
    await _mark_trust(db_session, sid, "T2->T3")
    await _grant(db_session, PillarName.PERSONAL_FINANCE, "categorize_transaction")
    await db_session.commit()
    r = await _gateway(db_session, sid, action_name="wire_transfer", pillar="personal_finance")
    assert r["status"] == "blocked" and r["reason"] == "no_approval"


@pytest.mark.asyncio
async def test_out_of_bounds_falls_through(db_session, test_session_row):
    sid = test_session_row.id
    await _mark_trust(db_session, sid, "T2->T3")
    await _grant(db_session, PillarName.PERSONAL_FINANCE, "categorize_transaction",
                 bounds={"max_amount": 100})
    await db_session.commit()
    # request amount exceeds the grant's bound
    r = await _gateway(db_session, sid, action_name="categorize_transaction",
                       pillar="personal_finance", payload={"amount": 500})
    assert r["status"] == "blocked" and r["reason"] == "no_approval"


@pytest.mark.asyncio
async def test_unrecognized_bound_fails_closed(db_session, test_session_row):
    """Bug fixed: an unverifiable bound (a non-max_ key) must DENY, not be
    silently ignored — otherwise an unenforceable limit fails open."""
    sid = test_session_row.id
    await _mark_trust(db_session, sid, "T2->T3")
    await _grant(db_session, PillarName.PERSONAL_FINANCE, "categorize_transaction",
                 bounds={"min_amount": 100})   # not a max_ constraint
    await db_session.commit()
    r = await _gateway(db_session, sid, action_name="categorize_transaction",
                       pillar="personal_finance", payload={"amount": 50})
    assert r["status"] == "blocked" and r["reason"] == "no_approval"


@pytest.mark.asyncio
async def test_trust_below_t3_falls_through(db_session, test_session_row):
    sid = test_session_row.id
    await _mark_trust(db_session, sid, "T1->T2")     # only T2
    await _grant(db_session, PillarName.PERSONAL_FINANCE, "categorize_transaction")
    await db_session.commit()
    r = await _gateway(db_session, sid, action_name="categorize_transaction", pillar="personal_finance")
    assert r["status"] == "blocked" and r["reason"] == "no_approval"


# ---------------------------------------------------------------------------
# EC-36: >=3 real grants, each traceable to a written rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ec36_three_grants_operating_each_a_written_rule(db_session):
    # Proposals come from the STATIC classification (not approval history).
    proposals = await propose_standing_authorities(db_session)
    assert len(proposals) >= 3
    for p in proposals[:3]:
        await grant_standing_authority(
            pillar=p.pillar, action_type=p.action_type, bounds=p.bounds,
            rationale=p.rationale, granted_by="blake", renewal_date=_FUTURE, db=db_session,
        )
    await db_session.commit()

    grants = (await db_session.execute(
        select(StandingAuthority).where(StandingAuthority.status == "active")
    )).scalars().all()
    assert len(grants) >= 3
    for g in grants:
        assert g.granted_by == "blake"          # explicit source, never inferred
        assert g.reversible is True             # reversible-only
        assert g.rationale.strip()              # the written rule
        assert g.renewal_date is not None       # periodic renewal


# ---------------------------------------------------------------------------
# "Never inferred from repeated approvals"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_approvals_create_no_standing_authority(db_session, test_session_row):
    sid = test_session_row.id
    await _mark_trust(db_session, sid, "T2->T3")
    # Simulate repeated per-action approvals of the same action.
    for _ in range(10):
        db_session.add(ActionLog(
            session_id=sid, agent=AgentName.MASTER, action_type=ActionType.CONFIRM,
            user_confirmed=True, input_summary="approve categorize_transaction",
        ))
    await db_session.commit()

    # No grant was created by that history...
    n_grants = (await db_session.execute(
        select(func.count()).select_from(StandingAuthority)
    )).scalar_one()
    assert n_grants == 0
    # ...and the proposal generator still proposes only from the static
    # classification — approval frequency produced nothing.
    proposals = await propose_standing_authorities(db_session)
    assert all(p.action_type in ("categorize_transaction", "add_calendar_reminder", "draft_message")
               for p in proposals)


# ---------------------------------------------------------------------------
# Grant guards: reversible-only, sourced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_requires_reversible_and_source(db_session):
    with pytest.raises(ValueError):   # empty source
        await grant_standing_authority(
            pillar=PillarName.LEGAL, action_type="x", bounds={}, rationale="r",
            granted_by="  ", renewal_date=_FUTURE, db=db_session,
        )
    with pytest.raises(ValueError):   # non-reversible
        await grant_standing_authority(
            pillar=PillarName.LEGAL, action_type="x", bounds={}, rationale="r",
            granted_by="blake", renewal_date=_FUTURE, db=db_session, reversible=False,
        )


@pytest.mark.asyncio
async def test_db_check_rejects_non_reversible_grant(db_session):
    db_session.add(StandingAuthority(
        pillar=PillarName.LEGAL, action_type="x", bounds={}, reversible=False,
        rationale="r", granted_by="blake", renewal_date=_FUTURE, status="active",
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()
