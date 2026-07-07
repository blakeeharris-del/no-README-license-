"""
aether.memory.standing_authority
==================================

Standing L4 authority (EC-36, Foundation §9.2 T3). A standing grant lets a
(pillar, action_type) run WITHOUT per-action approval — but only while
global trust >= T3, within the grant's bounds, before its renewal date,
and while active. Per-pillar scoped, so global T3 grants *eligibility*,
not blanket autonomy.

Layering: ``memory`` module — imports only ``models``. The skills-layer
gateway (``operational.action_gateway``) imports this to decide whether to
skip per-action approval.

--------------------------------------------------------------------------
STRUCTURAL ANTI-INFERENCE GUARANTEE ("never inferred from repeated
approvals", Blake's ruling / DP-10):

  1. A grant row exists ONLY via ``grant_standing_authority``, which
     requires a non-empty ``granted_by`` (ValueError) and the schema's
     ``granted_by NOT NULL`` — no anonymous grant is possible.
  2. ``propose_standing_authorities`` derives candidates from
     ``_STANDING_ELIGIBLE`` — a STATIC, hand-authored classification of
     routine/bounded/reversible (pillar, action_type) pairs — minus the
     grants that already exist. It reads NOTHING else. In particular it
     NEVER queries ``action_log`` or ``pending_escalations``, so approval
     frequency cannot become a proposal, let alone a grant. This module
     imports neither table; the guarantee is structural, not a convention.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.enums import PillarName
from aether.models.runtime import StandingAuthority

logger = logging.getLogger("aether.memory.standing_authority")

_T3_RANK = 3
_STAGE_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}


# Static routine/bounded/reversible classification — the ONLY source of
# proposal candidates (besides existing grants). Hand-authored; not derived
# from any approval history. Each entry is a candidate written rule.
_STANDING_ELIGIBLE: dict[tuple[PillarName, str], dict] = {
    (PillarName.PERSONAL_FINANCE, "categorize_transaction"): {
        "bounds": {},
        "rationale": "Re-categorizing a transaction is routine and fully reversible "
                     "(re-categorize back); no funds move. Reversible by design (DP-08).",
    },
    (PillarName.LEGAL, "add_calendar_reminder"): {
        "bounds": {"max_days_ahead": 365},
        "rationale": "Creating a legal-deadline calendar reminder is routine, bounded "
                     "(<=1yr out), and reversible (delete the reminder). DP-08.",
    },
    (PillarName.RELATIONSHIPS, "draft_message"): {
        "bounds": {},
        "rationale": "Drafting (not sending) a message is routine and reversible "
                     "(discard the draft); nothing leaves the system. DP-08.",
    },
}


@dataclass
class StandingProposal:
    pillar: PillarName
    action_type: str
    bounds: dict
    rationale: str


def _rank(stage: str) -> int:
    return _STAGE_RANK.get(stage, 0)


def _within_bounds(request: dict, bounds: dict) -> bool:
    """A request is within bounds iff every ``max_<field>`` limit in the
    grant is satisfied by ``request['<field>']``. Empty bounds = no limit.

    FAIL-CLOSED (bug found in adversarial review, fixed here): this gates
    an external action, so an UNVERIFIABLE bound must DENY, not be ignored.
    A missing request value for a declared limit is out of bounds; and any
    bound key that is not a recognized ``max_<field>`` constraint (a typo,
    a ``min_``, or a shape this checker doesn't understand) returns False
    rather than silently passing — otherwise an unenforceable bound would
    fail open and authorize actions it was meant to limit."""
    for key, limit in (bounds or {}).items():
        if not key.startswith("max_"):
            logger.warning(
                "standing grant has an unrecognized bound %r; denying (fail-closed)", key
            )
            return False
        field = key[len("max_"):]
        value = request.get(field)
        if value is None or value > limit:
            return False
    return True


async def propose_standing_authorities(db: AsyncSession) -> list[StandingProposal]:
    """Candidate grants from the STATIC eligibility classification, minus
    what is already granted. Reads ``_STANDING_ELIGIBLE`` + existing grants
    ONLY — never approval history (see module anti-inference guarantee)."""
    existing = {
        (g.pillar, g.action_type)
        for g in (await db.execute(
            select(StandingAuthority).where(StandingAuthority.status == "active")
        )).scalars().all()
    }
    return [
        StandingProposal(pillar=p, action_type=a, bounds=spec["bounds"], rationale=spec["rationale"])
        for (p, a), spec in _STANDING_ELIGIBLE.items()
        if (p, a) not in existing
    ]


async def grant_standing_authority(
    *,
    pillar: PillarName,
    action_type: str,
    bounds: dict,
    rationale: str,
    granted_by: str,
    renewal_date: datetime,
    db: AsyncSession,
    reversible: bool = True,
) -> StandingAuthority:
    """The ONLY way a standing grant is created (Blake's explicit approve
    path). Requires a non-empty source and reversible=true (both also
    enforced by the schema). Never derives anything from approval history."""
    if not granted_by or not granted_by.strip():
        raise ValueError("grant_standing_authority requires a non-empty granted_by source")
    if not reversible:
        raise ValueError("only reversible actions qualify for standing authority (§9.2, DP-08)")
    if not rationale or not rationale.strip():
        raise ValueError("a standing authority requires a written rationale (the written rule)")

    grant = StandingAuthority(
        pillar=pillar, action_type=action_type, bounds=bounds or {},
        reversible=reversible, rationale=rationale, granted_by=granted_by,
        renewal_date=renewal_date, status="active",
    )
    db.add(grant)
    await db.flush()
    logger.info("standing authority granted: %s/%s by %s (renewal %s)",
                pillar.value, action_type, granted_by, renewal_date.isoformat())
    return grant


async def find_valid_standing_grant(
    action_name: str | None,
    pillar: str | None,
    trust_stage: str,
    request: dict,
    db: AsyncSession,
) -> StandingAuthority | None:
    """Return the standing grant that authorizes this action, or None.

    ``action_name`` is the SPECIFIC action the grant scopes (matched against
    ``standing_authorities.action_type``) — distinct from the abstract
    authority-matrix action class.

    ALL conditions must hold (a narrow, all-must-hold bypass):
      - global trust_stage >= T3        (the T-model precondition)
      - an ACTIVE grant covers (action_name, pillar)
      - now() < renewal_date            (not lapsed by date)
      - the request is within the grant's bounds
    Any failure returns None -> the gateway falls through to per-action
    approval (the unchanged default)."""
    if _rank(trust_stage) < _T3_RANK:
        return None
    if pillar is None or action_name is None:
        return None
    try:
        pillar_enum = PillarName(pillar)
    except ValueError:
        return None

    grant = (await db.execute(
        select(StandingAuthority).where(
            StandingAuthority.status == "active",
            StandingAuthority.pillar == pillar_enum,
            StandingAuthority.action_type == action_name,
        )
    )).scalars().first()
    if grant is None:
        return None

    renewal = grant.renewal_date
    if renewal.tzinfo is None:
        renewal = renewal.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= renewal:
        return None  # lapsed by date

    if not _within_bounds(request or {}, grant.bounds or {}):
        return None

    return grant
