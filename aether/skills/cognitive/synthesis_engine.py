"""
aether.skills.cognitive.synthesis_engine
===========================================

SKILL-04 (Missing Specs). Given L2 nodes for one pillar, produce
candidate L3 belief nodes via an LLM call. This skill does NOT write
nodes — it returns candidates for ``run_synthesis()`` to write through
``write_node()`` (which is where INV enforcement actually happens).

Prompt: tries ``prompts/synthesis_{pillar}.txt`` first (so a
pillar-specific prompt can be dropped in later with no code change),
falling back to the parameterized ``prompts/synthesis.txt``.

Postconditions enforced here (belt to write_node()'s braces):
  - candidates never carry confidence="explicit" (INV-03);
  - synthesis_from is non-empty for every returned candidate;
  - on any LLM failure, returns an empty result rather than raising
    (FM-01/FM-02/FM-03).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aether.skills._llm import call_json

logger = logging.getLogger("aether.skills.cognitive.synthesis_engine")

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "agents" / "prompts"
_L2_CONTENT_CHARS = 500          # spec: truncate each l2 node content to 500 chars
_MAX_L2_NODES = 40               # FM-03 guard: keep the prompt within budget
_EMPTY = {"candidates": [], "contradictions_found": []}


def _load_prompt(pillar: str) -> str:
    specific = _PROMPTS_DIR / f"synthesis_{pillar}.txt"
    base = _PROMPTS_DIR / "synthesis.txt"
    template = specific.read_text() if specific.exists() else base.read_text()
    return template.replace("{pillar}", pillar)


def _summarize(nodes: list[dict], *, content: bool) -> list[dict]:
    out = []
    for n in nodes:
        item = {"id": str(n.get("id", "")), "title": n.get("title", "")}
        if content:
            item["content"] = (n.get("content", "") or "")[:_L2_CONTENT_CHARS]
        out.append(item)
    return out


def _sanitize(parsed: dict) -> dict:
    """Enforce postconditions on the LLM output."""
    candidates = []
    for c in parsed.get("candidates", []) or []:
        conf = c.get("confidence")
        if conf == "explicit":               # INV-03: never explicit
            conf = "inferred"
        if conf not in ("inferred", "speculative"):
            conf = "speculative"
        synthesis_from = [str(x) for x in (c.get("synthesis_from") or [])]
        if not synthesis_from:               # postcondition: must be non-empty
            continue                          # drop unsupported candidates
        candidates.append(
            {
                "title": c.get("title", ""),
                "content": c.get("content", ""),
                "confidence": conf,
                "synthesis_from": synthesis_from,
                "supersedes_l3_id": c.get("supersedes_l3_id"),
                "candidate_type": c.get("candidate_type", "new_belief"),
            }
        )
    contradictions = [
        {"candidate_index": int(x.get("candidate_index", 0)),
         "conflicts_with_id": str(x.get("conflicts_with_id", ""))}
        for x in (parsed.get("contradictions_found") or [])
    ]
    return {"candidates": candidates, "contradictions_found": contradictions}


async def run_synthesis_engine(inputs: dict, db) -> dict:
    """
    inputs: ``{"pillar", "l2_nodes": [NodeSummary], "existing_l3": [NodeSummary],
               "session_id"}``. Returns ``{"candidates", "contradictions_found"}``.
    """
    pillar = inputs.get("pillar", "")
    l2_nodes = inputs.get("l2_nodes") or []
    existing_l3 = inputs.get("existing_l3") or []

    if not l2_nodes:                          # FM-01
        return dict(_EMPTY)

    truncated = len(l2_nodes) > _MAX_L2_NODES
    l2_nodes = l2_nodes[:_MAX_L2_NODES]

    system = _load_prompt(pillar)
    user = json.dumps(
        {
            "pillar": pillar,
            "l2_nodes": _summarize(l2_nodes, content=True),
            "existing_l3": _summarize(existing_l3, content=False),  # titles only
        }
    )

    parsed = await call_json(system, user, logger=logger, max_tokens=4096, temperature=0.3)
    if parsed is None:                        # FM-02
        return dict(_EMPTY)

    result = _sanitize(parsed)
    if truncated and result["candidates"]:    # FM-03: note truncation
        result["candidates"][0]["content"] = (
            "[note: L2 node set truncated to fit token budget] "
            + result["candidates"][0]["content"]
        )
    return result
