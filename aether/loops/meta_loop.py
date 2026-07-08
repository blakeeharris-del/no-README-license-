"""
aether.loops.meta_loop
=========================

The Meta-Loop (Implementation Plan §16.7). Evaluates the health of the
other loops and writes a scorecard to ``meta_loop_runs``.

Phase-1 scope (EC-24): produce AT LEAST ONE real loop-health scorecard
from actual ``skill_performance`` and ``loop_runs`` data. Full
200-cycle stability across all seven loop types is Phase-2's bar and is
deliberately out of scope here (AETHER_PHASE1_PROMPT §3, Discrepancy D).

It builds the scorecard by running ``evaluative.loop_health_checker``
(which aggregates loop_runs) and folds in ``skill_performance``
below-threshold signals, so the scorecard is grounded in both data
sources EC-24 names.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.runtime import MetaLoopRun, SkillPerformance
from aether.skills.evaluative.loop_health_checker import check_loop_health

logger = logging.getLogger("aether.loops.meta_loop")


class MetaLoop:
    async def run(
        self, db: AsyncSession, *, lookback_days: int = 7, triggered_by: str = "manual",
        window_end: datetime | None = None,
    ) -> MetaLoopRun:
        # Loop-runs half of the scorecard (called directly — a system loop,
        # not a session-scoped skill invocation). ``window_end`` (EC-37)
        # defaults to now, preserving the single-sliding-window behavior.
        health = await check_loop_health(
            {"lookback_days": lookback_days, "window_end": window_end}, db
        )
        scorecard = health["scorecard"]
        anomalies = list(health["anomalies"])
        improvement_signals = list(health["improvement_signals"])

        # skill_performance half: fold in any below-threshold skills within
        # the SAME window as the loop-health half.
        w_end = window_end or datetime.now(timezone.utc)
        if w_end.tzinfo is None:
            w_end = w_end.replace(tzinfo=timezone.utc)
        since = w_end - timedelta(days=lookback_days)
        flagged = (
            await db.execute(
                select(SkillPerformance).where(
                    SkillPerformance.below_threshold.is_(True),
                    SkillPerformance.computed_at >= since,
                    SkillPerformance.computed_at <= w_end,
                )
            )
        ).scalars().all()
        for sp in flagged:
            anomalies.append({
                "loop_type": "skill", "metric": "below_threshold",
                "value": float(sp.error_rate or 0), "threshold": 0.05, "severity": "warning",
            })
            improvement_signals.append(f"Skill {sp.skill_name} is below performance threshold.")

        run = MetaLoopRun(
            lookback_days=lookback_days,
            loop_health_scorecard=scorecard,
            anomalies_detected=anomalies,
            improvement_signals=improvement_signals,
            reviewed_by_user=False,
            triggered_by=triggered_by,
        )
        db.add(run)
        await db.flush()
        logger.info("meta_loop: scorecard with %d loop types, %d anomalies",
                    len(scorecard), len(anomalies))
        return run
