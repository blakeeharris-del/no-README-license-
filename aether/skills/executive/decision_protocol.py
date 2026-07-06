"""
aether.skills.executive.decision_protocol
============================================

SKILL-22 (Missing Specs) — the real implementation replacing Phase-0's
structural placeholder (EC-27, Discrepancy C). Executes steps 1-4 of
the Decision Protocol (Sense -> Analyze -> Challenge -> Recommend);
steps 5-7 (Await -> Execute -> Log) run in the goal loop.

GOVERNANCE PRECEDENCE — recommendation deferral:
Missing Specs SKILL-22's output schema carries a populated
``recommendation`` field, and its "7-step" framing includes a Recommend
step. Foundation §10.6, which governs, states the opposite: "The
Decision Protocol does not produce a recommendation by default. It
produces a structured decision package... [the user] may ask Aether to
recommend a path — at which point the Master Agent produces a
recommendation." Foundation wins. So by default this skill DEFERS the
recommendation (a neutral message pointing the user to the analysis and
inviting an explicit ask), and only produces a real recommendation when
``user_intent`` explicitly requests one. Foundation's "always surfaces
what Aether does not know" is honored by the ``challenge`` field, which
is always non-empty for any action affecting external state.

Placement (Discrepancy C): Foundation §10.7.1 lists decision_protocol
as an *executive skill*, so it lives here in skills/executive/, not in
the agents layer where Phase-0 parked the shell.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aether.schemas.agent import ApprovalRequest
from aether.skills._llm import call_json

logger = logging.getLogger("aether.skills.executive.decision_protocol")

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "agents" / "prompts"
_DEFER = (
    "No recommendation is offered by default. Review the analysis and the flagged "
    "concerns above, then ask Aether to recommend a path if you would like one."
)

# UserIntent.action_type -> estimated authority (mirrors MasterAgent's
# _estimate_authority: query/review/clarify -> L0, synthesize -> L1,
# write -> L2, task -> L3). Used to decide auto_executable / approval.
_AUTHORITY_BY_ACTION = {
    "query": 0, "review": 0, "clarify": 0, "synthesize": 1, "write": 2, "task": 3,
}


def _wants_recommendation(user_intent: dict) -> bool:
    """Foundation §10.6: recommend only when the user explicitly asks."""
    if user_intent.get("wants_recommendation") is True:
        return True
    text = (user_intent.get("raw_input", "") + " " + user_intent.get("subject", "")).lower()
    return any(k in text for k in ("recommend", "what should i", "which should i", "advise"))


def _system_prompt(pillars: list[str]) -> str:
    base = (_PROMPTS_DIR / "base_system.txt").read_text()
    addenda = []
    for pillar in pillars or []:
        pfile = _PROMPTS_DIR / "pillar" / f"{pillar}.txt"
        if pfile.exists():
            addenda.append(pfile.read_text())
    framing = (
        "\n\nTASK: Run the Decision Protocol (Sense, Analyze, Challenge) on the "
        "proposed action. Do NOT produce a recommendation. Surface what is unknown. "
        "Return ONLY JSON:\n"
        '{"sense_summary":str,"analysis":str,"challenge":str,'
        '"recommendation_if_asked":str}\n'
        "Use only facts in the provided nodes."
    )
    return base + ("\n\n" + "\n\n".join(addenda) if addenda else "") + framing


async def run_decision_protocol(inputs: dict, db) -> dict:
    """
    inputs: ``{"proposed_action","relevant_nodes":[NodeSummary],
               "user_intent":UserIntent,"session_id"}``.
    Returns the decision brief (see SKILL-22 outputs).
    """
    proposed = inputs.get("proposed_action", "")
    relevant_nodes = inputs.get("relevant_nodes") or []
    user_intent = inputs.get("user_intent") or {}
    pillars = user_intent.get("implied_pillars") or []

    authority_level = _AUTHORITY_BY_ACTION.get(user_intent.get("action_type"), 3)
    auto_executable = authority_level <= 2
    approval_required = not auto_executable

    system = _system_prompt(pillars)
    user = json.dumps({
        "proposed_action": proposed,
        "user_intent": user_intent,
        "relevant_nodes": [
            {"id": str(n.get("id", "")), "title": n.get("title", ""),
             "content": (n.get("content", "") or "")[:400]} for n in relevant_nodes
        ],
    })
    parsed = await call_json(system, user, logger=logger, max_tokens=1024, temperature=0.3)

    if parsed is None:
        sense = f"Proposed action: {proposed}."
        analysis = "Analysis unavailable (LLM error)."
        challenge = "Aether could not analyze this action; do not proceed without review."
        rec_if_asked = ""
    else:
        sense = parsed.get("sense_summary") or f"Proposed action: {proposed}."
        analysis = parsed.get("analysis") or ""
        challenge = parsed.get("challenge") or ""
        rec_if_asked = parsed.get("recommendation_if_asked") or ""

    # challenge must be non-empty for any external-state action.
    if approval_required and not challenge.strip():
        challenge = "This action affects external state; confirm intent and review risks before proceeding."

    # Foundation §10.6: defer recommendation unless explicitly requested.
    recommendation = rec_if_asked if _wants_recommendation(user_intent) and rec_if_asked else _DEFER

    approval_request = None
    if approval_required:
        approval_request = ApprovalRequest(
            action=proposed[:200],
            target=", ".join(pillars) or "unspecified",
            amount_or_consequence=analysis[:200] or "See analysis.",
            timing=user_intent.get("urgency", "standard"),
            authority_level=authority_level,
            risk_level="restricted" if authority_level >= 3 else "medium",
        ).model_dump(mode="json")

    return {
        "sense_summary": sense,
        "analysis": analysis,
        "challenge": challenge,
        "recommendation": recommendation,
        "approval_required": approval_required,
        "approval_request": approval_request,
        "auto_executable": auto_executable,
    }
