"""
aether.agents.decision_protocol
===================================

Section 3's build order lists this file (Step 19, alongside
``master_agent.py``), but no section of the Phase-0 Prompt gives it a
numbered spec, and — notably — ``MasterAgent.process()``'s own 10
steps (Section 18) never call anything here. This mirrors
``rollback_executor.py``/``approval_enforcer.py``'s situation at the
skills-layer checkpoint: a file the repo structure requires to exist,
with no Phase-0 wiring instructions.

What's implemented here is a direct, minimal translation of
Foundation §10.6's conceptual description: "assembles the relevant
context, identifies options, evaluates tradeoffs, flags risks, and
presents the decision package with confidence levels attached... does
not produce a recommendation by default... always surfaces what
Aether does not know." Since ``challenge_and_prepare`` is a real
routing mode ``context_assembler`` already handles (it fetches
deadlines and tasks for that mode), this is offered as a hook a future
integration could call when that routing mode is active — but it is
NOT currently invoked by ``MasterAgent.process()``, since Section 18
never says to call it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DecisionOption(BaseModel):
    description: str
    tradeoffs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: str = "inferred"


class DecisionPackage(BaseModel):
    """
    A structured decision package: options with tradeoffs/risks
    attached, confidence gaps surfaced explicitly, and deliberately no
    ``recommendation`` field populated by default (Foundation §10.6:
    "does not produce a recommendation by default").
    """

    options: list[DecisionOption] = Field(default_factory=list)
    confidence_gaps: list[str] = Field(default_factory=list)
    recommendation: str | None = None


def build_decision_package(context_packet: dict) -> DecisionPackage:
    """
    Structures a bare decision-package shell from a ``challenge_and_prepare``
    ContextPacket. Does not itself call an LLM — actually populating
    ``options``/``tradeoffs``/``risks`` with real analysis is exactly
    the kind of reasoning ``MasterAgent._llm_call()`` does; this
    function's role would be assembling the *shape* the LLM is asked to
    fill in, not replacing that reasoning. Returns an empty package
    (with confidence_gaps populated) when there isn't enough context to
    even start.
    """
    nodes = context_packet.get("relevant_nodes", {}).get("nodes", [])
    if not nodes:
        return DecisionPackage(
            confidence_gaps=["No relevant memory nodes were found for this decision."]
        )
    return DecisionPackage(
        options=[],
        confidence_gaps=[
            "No options have been generated yet — this shell is populated by the "
            "LLM reasoning step, not by this function."
        ],
    )
