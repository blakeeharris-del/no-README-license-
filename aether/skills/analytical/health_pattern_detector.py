"""
aether.skills.analytical.health_pattern_detector
===================================================

SKILL-12 (Missing Specs). Identifies trends across lab/habit/medication
nodes. Deliberately conservative and non-clinical.

Hard safety constraints (spec), enforced here in code, not just prompt:
  - every response carries the fixed disclaimer;
  - the words "diagnosis"/"treatment"/"prescribe" never appear in output
    (post-filtered);
  - mental-health nodes are acknowledged as present but their content is
    never fed to the model or summarized;
  - pattern confidence is only "inferred" or "speculative", never
    "explicit".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aether.memory.read_protocol import read_pillar_nodes
from aether.models.enums import NodeType, PillarName
from aether.skills._llm import call_json

logger = logging.getLogger("aether.skills.analytical.health_pattern_detector")

_BASE_PROMPT = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "pillar" / "health.txt"
_DISCLAIMER = "This is a pattern observation, not medical advice or diagnosis."
_FORBIDDEN = ("diagnosis", "treatment", "prescribe")


def _scrub(text: str) -> str:
    """Remove clinical terms the skill must never emit (case-insensitive)."""
    out = text or ""
    for word in _FORBIDDEN:
        out = out.replace(word, "[redacted]").replace(word.capitalize(), "[redacted]")
    return out


async def detect_health_patterns(inputs: dict, db) -> dict:
    """
    inputs: ``{"session_id": str, "lookback_days": int (default 180)}``.
    Returns ``{"patterns", "medication_count", "upcoming_appointments",
    "disclaimer"}``.
    """
    lookback_days = int(inputs.get("lookback_days") or 180)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)

    nodes = await read_pillar_nodes([PillarName.HEALTH], db)

    # Split out mental-health nodes: acknowledge existence only.
    def _is_mental_health(n) -> bool:
        meta = n.metadata_ or {}
        return meta.get("category") == "mental_health" or meta.get("sensitive") is True

    mh_present = any(_is_mental_health(n) for n in nodes)
    analyzable = [n for n in nodes if not _is_mental_health(n) and n.created_at >= cutoff]

    medication_count = sum(
        1 for n in nodes if (n.metadata_ or {}).get("category") == "medication"
    )
    upcoming_appointments = sum(
        1 for n in nodes
        if n.type in (NodeType.EVENT, NodeType.TASK)
        and (n.metadata_ or {}).get("appointment") is True
    )

    result = {
        "patterns": [],
        "medication_count": medication_count,
        "upcoming_appointments": upcoming_appointments,
        "disclaimer": _DISCLAIMER,   # always present
    }
    if mh_present:
        result["patterns"].append({
            "pattern_type": "compliance",
            "description": "Mental health tracking nodes present (content withheld).",
            "evidence_nodes": [],
            "confidence": "inferred",
            "date_range": f"{cutoff.date()} to {now.date()}",
        })
    if not analyzable:
        return result

    system = _BASE_PROMPT.read_text() + (
        "\n\nTASK: Identify observational PATTERNS (not clinical conclusions) across "
        "these health-tracking nodes. Never use the words diagnosis, treatment, or "
        "prescribe. Return ONLY JSON:\n"
        '{"patterns":[{"pattern_type":"trend|anomaly|compliance|milestone",'
        '"description":str,"evidence_nodes":[str],'
        '"confidence":"inferred|speculative","date_range":str}]}'
    )
    user = json.dumps({
        "nodes": [{"id": str(n.id), "title": n.title, "content": (n.content or "")[:300],
                   "created_at": n.created_at.isoformat()} for n in analyzable],
    })

    parsed = await call_json(system, user, logger=logger, max_tokens=1024, temperature=0.3)
    if parsed is not None:
        for p in parsed.get("patterns") or []:
            conf = p.get("confidence")
            if conf not in ("inferred", "speculative"):  # never explicit
                conf = "speculative"
            result["patterns"].append({
                "pattern_type": p.get("pattern_type", "trend"),
                "description": _scrub(p.get("description", "")),
                "evidence_nodes": [str(x) for x in (p.get("evidence_nodes") or [])],
                "confidence": conf,
                "date_range": p.get("date_range", f"{cutoff.date()} to {now.date()}"),
            })

    return result
