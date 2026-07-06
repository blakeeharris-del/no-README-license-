"""
aether.skills.evaluative.skill_performance_tracker
====================================================

SKILL-27 (Missing Specs). Computes rolling 30-day performance metrics
for skills invoked in the current session and UPSERTs them into
``skill_performance``; flags skills that breach thresholds (EC-20).

Honesty note on ``accuracy_score``: skill_invocation_log records
success/timeout/error and latency, but NOT output correctness — there
is no ground-truth signal in Phase-1 from which a true accuracy could
be derived. So ``error_rate`` (failure-rate) and ``p95_latency_ms`` are
computed for real; ``accuracy_score`` and ``override_rate`` are left
NULL rather than fabricated. EC-20's "real ... failure-rate/latency
data" is satisfied by real values; accuracy stays honestly absent.

Window key: window_start/window_end are truncated to the day so the
(skill_name, window_start, window_end) UPSERT key is stable within a
day and the rolling window advances daily.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from aether.models.enums import EscalationStatus, EscalationType, PriorityClass
from aether.models.runtime import PendingEscalation, SkillInvocationLog

logger = logging.getLogger("aether.skills.evaluative.skill_performance_tracker")

_ERROR_FLAG = 0.05
_OVERRIDE_FLAG = 0.10


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


async def track_skill_performance(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str}``.
    Returns ``{"skills_evaluated", "skills_flagged", "performance_records_written"}``.
    """
    session_id = inputs.get("session_id")
    now = datetime.now(timezone.utc)
    window_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = window_end - timedelta(days=30)

    # Which skills were invoked this session.
    session_skills = set(
        (await db.execute(
            select(SkillInvocationLog.skill_name).where(SkillInvocationLog.session_id == session_id)
        )).scalars().all()
    )
    if not session_skills:
        return {"skills_evaluated": 0, "skills_flagged": [], "performance_records_written": 0}

    from aether.skills.registry import SKILL_TIMEOUTS

    skills_flagged = []
    records_written = 0

    for skill_name in sorted(session_skills):
        rows = list((await db.execute(
            select(SkillInvocationLog.status, SkillInvocationLog.latency_ms).where(
                SkillInvocationLog.skill_name == skill_name,
                SkillInvocationLog.timestamp >= window_start,
            )
        )).all())
        total = len(rows)
        if total == 0:
            continue
        errors = sum(1 for status, _ in rows if status in ("error", "timeout"))
        error_rate = round(errors / total, 4)
        latencies = [lat for _, lat in rows if lat is not None]
        p95 = _p95(latencies)

        below_threshold = error_rate > _ERROR_FLAG

        # Latency flag: p95 over 80% of the configured timeout.
        timeout_ms = SKILL_TIMEOUTS.get(skill_name)
        if p95 is not None and timeout_ms and p95 > timeout_ms * 0.8:
            below_threshold = True
            skills_flagged.append({"skill_name": skill_name, "metric": "p95_latency_ms",
                                   "value": float(p95), "threshold": timeout_ms * 0.8})
        if error_rate > _ERROR_FLAG:
            skills_flagged.append({"skill_name": skill_name, "metric": "error_rate",
                                   "value": error_rate, "threshold": _ERROR_FLAG})

        await db.execute(
            text("""
                INSERT INTO skill_performance
                    (skill_name, window_start, window_end, invocation_count,
                     error_rate, p95_latency_ms, below_threshold)
                VALUES (:name, :ws, :we, :count, :err, :p95, :below)
                ON CONFLICT (skill_name, window_start, window_end) DO UPDATE SET
                    invocation_count = EXCLUDED.invocation_count,
                    error_rate       = EXCLUDED.error_rate,
                    p95_latency_ms   = EXCLUDED.p95_latency_ms,
                    below_threshold  = EXCLUDED.below_threshold,
                    computed_at      = now()
            """),
            {"name": skill_name, "ws": window_start, "we": window_end,
             "count": total, "err": error_rate, "p95": p95, "below": below_threshold},
        )
        records_written += 1

    # Flagged skills -> p3 escalation.
    for flag in skills_flagged:
        db.add(PendingEscalation(
            escalation_type=EscalationType.CLARIFICATION,
            priority_class=PriorityClass.P3,
            content={"title": "Skill performance below threshold",
                     "description": f"{flag['skill_name']} {flag['metric']}={flag['value']}",
                     "skill_name": flag["skill_name"]},
            session_id=session_id, status=EscalationStatus.PENDING,
        ))
    if skills_flagged:
        await db.flush()

    return {
        "skills_evaluated": records_written,
        "skills_flagged": skills_flagged,
        "performance_records_written": records_written,
    }
