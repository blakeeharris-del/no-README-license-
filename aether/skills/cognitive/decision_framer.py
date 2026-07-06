"""
aether.skills.cognitive.decision_framer
==========================================

SKILL-05 (Missing Specs). Structures a proposed user action into a
decision brief — options, assumptions, risks, a recommendation, and
what's missing — via an LLM call. Used in the Challenge & Prepare
routing mode.

Safety constraints (spec): the output must state plainly that it is a
recommendation and not a decision, and must not claim access to
information outside ``relevant_nodes``. The recommendation clause is
appended deterministically here so it cannot be omitted by the model.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aether.skills._llm import call_json

logger = logging.getLogger("aether.skills.cognitive.decision_framer")

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "agents" / "prompts"
_DECISION_DISCLAIMER = "This is a recommendation, not a decision. You decide."


def _system_prompt(pillars_affected: list[str]) -> str:
    base = (_PROMPTS_DIR / "base_system.txt").read_text()
    addenda = []
    for pillar in pillars_affected or []:
        pfile = _PROMPTS_DIR / "pillar" / f"{pillar}.txt"
        if pfile.exists():
            addenda.append(pfile.read_text())
    framing = (
        "\n\nTASK: Frame the proposed decision. Return ONLY JSON:\n"
        '{"options":[{"label":str,"description":str,"pros":[str],"cons":[str]}],'
        '"assumptions":[str],"risks":[str],"recommendation":str,'
        '"confidence":"high|medium|low","missing_info":[str]}\n'
        "Only use facts present in the provided nodes. Do not claim access to "
        "anything not given. Order risks by severity."
    )
    return base + ("\n\n" + "\n\n".join(addenda) if addenda else "") + framing


async def frame_decision(inputs: dict, db) -> dict:
    """
    inputs: ``{"proposed_action", "relevant_nodes": [NodeSummary],
               "pillars_affected": [str], "urgency"}``.
    Returns the decision brief dict (see SKILL-05 outputs).
    """
    proposed = inputs.get("proposed_action", "")
    relevant_nodes = inputs.get("relevant_nodes") or []
    pillars_affected = inputs.get("pillars_affected") or []
    urgency = inputs.get("urgency", "standard")

    system = _system_prompt(pillars_affected)
    user = json.dumps(
        {
            "proposed_action": proposed,
            "urgency": urgency,
            "relevant_nodes": [
                {"id": str(n.get("id", "")), "title": n.get("title", ""),
                 "content": (n.get("content", "") or "")[:500]}
                for n in relevant_nodes
            ],
        }
    )

    parsed = await call_json(system, user, logger=logger, max_tokens=2048, temperature=0.3)
    if parsed is None:
        # Degrade: an honest empty frame that still carries the disclaimer
        # and flags that no analysis could be produced.
        return {
            "options": [],
            "assumptions": [],
            "risks": [],
            "recommendation": f"Unable to frame this decision right now. {_DECISION_DISCLAIMER}",
            "confidence": "low",
            "missing_info": ["Decision framing was unavailable (LLM error)."],
        }

    recommendation = (parsed.get("recommendation") or "").strip()
    if _DECISION_DISCLAIMER not in recommendation:
        recommendation = (recommendation + " " + _DECISION_DISCLAIMER).strip()

    return {
        "options": parsed.get("options") or [],
        "assumptions": parsed.get("assumptions") or [],
        "risks": parsed.get("risks") or [],
        "recommendation": recommendation,
        "confidence": parsed.get("confidence", "low"),
        "missing_info": parsed.get("missing_info") or [],
    }
