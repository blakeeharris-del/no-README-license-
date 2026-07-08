"""
tests.loops.test_ec37_meta_scorecards — EC-37.

Three consecutive weekly Meta-Loop scorecards showing no loop degradation
— a genuine three-cycle run, NOT "one scorecard counted three ways".

Three separate MetaLoop.run() calls over three distinct, non-overlapping
backdated windows, each seeded with DIFFERENT underlying loop activity
(different sessions, loop-type mixes, volumes per window). "No degradation"
is proven BOTH ways: (a) absolute — no CRITICAL anomaly in any of the
three scorecards (§16.7 thresholds); (b) trend — the key degradation rates
(forced_termination, safety_invocation, correction) do not worsen
window-over-window.

Window placement: the loop-health check reads loop_runs globally, and
ambient committed loop_runs always sit at ~now. So the three windows are
placed >=15 days in the past (ends at -35/-25/-15 days, each a 7-day
window) — deep enough that no ambient (always-recent) row can intrude,
keeping the simulation deterministic. This shifts the illustrative
-21/-14/-7 offsets deeper but preserves the property proven exactly:
three distinct non-overlapping backdated windows, different data, no
degradation both ways. The SAME mechanism runs on real data at G3 with
window_end defaulting to now (re-confirmation path).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aether.loops.meta_loop import MetaLoop
from aether.models.enums import LoopStatus, LoopType, SessionStatus
from aether.models.sessions import Session


async def _session(db):
    s = Session(status=SessionStatus.CLOSED, ended_at=datetime.now(timezone.utc))
    db.add(s)
    await db.flush()
    return s


async def _loop(db, session_id, loop_type, start_at, status=LoopStatus.COMPLETED):
    from aether.models.runtime import LoopRun

    db.add(LoopRun(
        loop_type=loop_type, trigger="ec37", session_id=session_id,
        status=status, iteration_count=1, max_iterations=10, max_duration_ms=120000,
        start_time=start_at, end_time=start_at + timedelta(seconds=5),
    ))
    await db.flush()


def _scorecard_metrics(scorecard: dict) -> dict:
    """The key degradation signals extracted from a real scorecard row."""
    forced = sum(v.get("forced_termination", 0) for v in scorecard.values())
    safety = scorecard.get("safety", {}).get("total_runs", 0)
    correction = scorecard.get("correction", {}).get("total_runs", 0)
    total = sum(v.get("total_runs", 0) for v in scorecard.values())
    return {"forced": forced, "safety": safety, "correction": correction, "total": total}


@pytest.mark.asyncio
async def test_ec37_three_weekly_scorecards_no_degradation(db_session):
    now = datetime.now(timezone.utc)

    # Three non-overlapping backdated 7-day windows (all >=15d old).
    w1_end = now - timedelta(days=35)   # window [-42, -35]
    w2_end = now - timedelta(days=25)   # window [-32, -25]
    w3_end = now - timedelta(days=15)   # window [-22, -15]

    # --- Window 1: different sessions, mix incl. 1 completed correction ---
    s1 = await _session(db_session)
    for d in (38, 39):
        await _loop(db_session, s1.id, LoopType.GOAL, now - timedelta(days=d))
    await _loop(db_session, s1.id, LoopType.CORRECTION, now - timedelta(days=40))

    # --- Window 2: more goal volume, a reflection; no corrections ---
    s2 = await _session(db_session)
    for d in (28, 29, 30, 31):
        await _loop(db_session, s2.id, LoopType.GOAL, now - timedelta(days=d))
    await _loop(db_session, s2.id, LoopType.REFLECTION, now - timedelta(days=27))

    # --- Window 3: different mix again, an escalation; no corrections ---
    s3 = await _session(db_session)
    for d in (18, 19, 20):
        await _loop(db_session, s3.id, LoopType.GOAL, now - timedelta(days=d))
    await _loop(db_session, s3.id, LoopType.ESCALATION, now - timedelta(days=17))
    await db_session.flush()

    # Three genuinely separate Meta-Loop runs, one per window.
    r1 = await MetaLoop().run(db_session, lookback_days=7, window_end=w1_end, triggered_by="scheduled")
    r2 = await MetaLoop().run(db_session, lookback_days=7, window_end=w2_end, triggered_by="scheduled")
    r3 = await MetaLoop().run(db_session, lookback_days=7, window_end=w3_end, triggered_by="scheduled")
    await db_session.commit()

    runs = [r1, r2, r3]
    metrics = [_scorecard_metrics(r.loop_health_scorecard) for r in runs]

    # The three cycles saw DIFFERENT data (not the same rows relabeled).
    assert metrics[0]["total"] == 3      # 2 goal + 1 correction
    assert metrics[1]["total"] == 5      # 4 goal + 1 reflection
    assert metrics[2]["total"] == 4      # 3 goal + 1 escalation
    assert len({m["total"] for m in metrics}) == 3

    # (a) ABSOLUTE: no CRITICAL anomaly in any of the three scorecards.
    for r in runs:
        criticals = [a for a in (r.anomalies_detected or []) if a.get("severity") == "critical"]
        assert criticals == [], f"unexpected critical anomaly: {criticals}"

    # (b) TREND: key degradation rates do not worsen window-over-window.
    for key in ("forced", "safety", "correction"):
        series = [m[key] for m in metrics]
        assert all(series[i + 1] <= series[i] for i in range(len(series) - 1)), \
            f"{key} worsened across cycles: {series}"
