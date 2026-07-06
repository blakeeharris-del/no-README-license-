"""
aether.skills.evaluative.loop_health_checker
===============================================

SKILL-28 (Missing Specs). Evaluates loop health over a lookback window
from loop_runs, producing a per-loop-type scorecard, anomalies, and
improvement signals. Consumed by the Meta-Loop (EC-24).

Discrepancy D note: the catalog tags this Phase-2. Per governance
precedence (EC-19 requires all 34 skills Active; EC-24 requires one
real scorecard in Phase-1), it is built and made Active in Phase-1. Its
full-stability bar (200 cycles across all 7 loop types) remains Phase-2
and is deliberately NOT asserted here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from aether.models.enums import LoopStatus, LoopType
from aether.models.runtime import LoopRun

logger = logging.getLogger("aether.skills.evaluative.loop_health_checker")


async def check_loop_health(inputs: dict, db) -> dict:
    """
    inputs: ``{"lookback_days": int (default 7)}``.
    Returns ``{"scorecard": {<loop_type>: {...}}, "anomalies": [...],
    "improvement_signals": [str]}``.
    """
    lookback_days = int(inputs.get("lookback_days") or 7)
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    runs = list((await db.execute(
        select(LoopRun).where(LoopRun.start_time >= since)
    )).scalars().all())

    scorecard: dict[str, dict] = {}
    anomalies = []
    improvement_signals = []

    for loop_type in LoopType:
        lt_runs = [r for r in runs if r.loop_type == loop_type]
        total = len(lt_runs)
        completed = sum(1 for r in lt_runs if r.status == LoopStatus.COMPLETED)
        forced = sum(1 for r in lt_runs if r.status == LoopStatus.FORCED_TERMINATION)
        avg_iter = round(sum(r.iteration_count for r in lt_runs) / total, 2) if total else 0.0
        correction_rate = round(forced / total, 4) if total else 0.0

        scorecard[loop_type.value] = {
            "total_runs": total,
            "completed": completed,
            "forced_termination": forced,
            "correction_rate": correction_rate,
            "avg_iterations": avg_iter,
        }

        # Anomaly thresholds (spec).
        if total and forced > 0:
            anomalies.append({"loop_type": loop_type.value, "metric": "forced_termination_rate",
                              "value": round(forced / total, 4), "threshold": 0.0,
                              "severity": "critical"})
            improvement_signals.append(
                f"{loop_type.value} loop had {forced} forced termination(s); investigate limits."
            )
        if loop_type == LoopType.SAFETY and total > 0:
            anomalies.append({"loop_type": "safety", "metric": "safety_invocation_rate",
                              "value": float(total), "threshold": 0.0, "severity": "critical"})
            improvement_signals.append("Safety loop fired — treat as an incident.")

    return {
        "scorecard": scorecard,
        "anomalies": anomalies,
        "improvement_signals": improvement_signals,
    }
