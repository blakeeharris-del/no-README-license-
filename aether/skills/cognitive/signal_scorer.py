"""
aether.skills.cognitive.signal_scorer
========================================

SKILL-03 (Phase-0 Prompt Section 15). Rule-based; no LLM. Scores a
signal on impact, time sensitivity, confidence, and noise penalty, and
derives a priority class from those four scores.
"""

from __future__ import annotations

from aether.models.enums import PriorityClass
from aether.schemas.skills import ScoredSignal


async def score_signal(inputs: dict, db) -> dict:
    """Score ``inputs['signal']`` per the exact rules in Section 15."""
    signal = inputs["signal"]
    sig_type = signal.get("type")
    pillar = signal.get("pillar")
    source = signal.get("source")
    days_until = signal.get("days_until")
    amount = signal.get("amount")

    impact = _score_impact(sig_type, pillar, days_until, amount)
    time_sensitivity = _score_time_sensitivity(sig_type, days_until)
    confidence = _score_confidence(source, sig_type)
    noise_penalty = _score_noise_penalty(sig_type, source, days_until)
    priority_class = _classify_priority(impact, time_sensitivity, confidence, noise_penalty)

    return ScoredSignal(
        signal=signal,
        impact=impact,
        time_sensitivity=time_sensitivity,
        confidence=confidence,
        noise_penalty=noise_penalty,
        priority_class=priority_class,
    ).model_dump(mode="json")


def _score_impact(sig_type, pillar, days_until, amount) -> int:
    if sig_type == "contradiction" or (sig_type == "deadline" and pillar == "legal"):
        return 5
    if sig_type == "anomaly" and amount is not None and amount > 10000:
        return 5
    if sig_type == "deadline" and pillar in ("personal_finance", "business"):
        return 4
    if sig_type == "task" and pillar == "legal":
        return 4
    if sig_type == "deadline" and days_until is not None and days_until <= 7:
        return 4
    if sig_type == "synthesis_result":
        return 3
    if sig_type == "task":
        return 2
    return 1


def _score_time_sensitivity(sig_type, days_until) -> int:
    if days_until is not None and days_until <= 1:
        return 5
    if days_until is not None and days_until <= 7:
        return 4
    if days_until is not None and days_until <= 30:
        return 3
    if days_until is None and sig_type == "anomaly":
        return 4
    if days_until is None:
        return 2
    return 1


def _score_confidence(source, sig_type) -> int:
    if source in ("user", "watchdog"):
        return 5
    if source == "system" and sig_type != "synthesis_result":
        return 4
    if source == "synthesis":
        return 3
    return 2


def _score_noise_penalty(sig_type, source, days_until) -> int:
    if sig_type == "synthesis_result" and source == "synthesis":
        return 2
    if sig_type == "task" and days_until is not None and days_until > 30:
        return 3
    return 0


def _classify_priority(impact, time_sensitivity, confidence, noise_penalty) -> PriorityClass:
    # Evaluated in the exact order given in Section 15: the first
    # matching rule wins, EXCEPT the explicit suppress overrides at the
    # end apply regardless of what the impact/time_sensitivity checks
    # above would have matched — "default OR noise_penalty >= 4 OR
    # confidence <= 1 -> suppress" reads as an override, not merely a
    # fallback, so it is checked first here rather than last.
    if noise_penalty >= 4 or confidence <= 1:
        return PriorityClass.SUPPRESS
    if impact >= 4 and time_sensitivity >= 4:
        return PriorityClass.P0
    if impact >= 3 and time_sensitivity >= 3:
        return PriorityClass.P1
    if impact >= 2:
        return PriorityClass.P2
    if impact >= 1 and noise_penalty < 3:
        return PriorityClass.P3
    return PriorityClass.SUPPRESS
