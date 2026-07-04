"""
aether.skills.cognitive.contradiction_detector
==================================================

SKILL-02 (Phase-0 Prompt Section 15). Detects only — never writes to
the DB, never modifies nodes. ``contradiction_enforcer`` (safety
skill) is what acts on the conflicts this returns.

Forward-reference note: reads ``agents/prompts/contradiction_surface.txt``
(Step 18) at call time — same deferred-file-read pattern as
``intent_parser.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic, APITimeoutError

from aether.config import settings
from aether.memory.read_protocol import fulltext_search
from aether.schemas.nodes import ConflictPair, ConflictResult

logger = logging.getLogger("aether.skills.cognitive.contradiction_detector")

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "agents" / "prompts" / "contradiction_surface.txt"
)
_SIMILARITY_THRESHOLD = 0.5


async def detect_contradiction(inputs: dict, db) -> dict:
    """
    inputs: ``{'candidate': {'title', 'content', 'pillar'}, 'pillar': str}``.
    Returns a ``ConflictResult`` dict.
    """
    candidate = inputs["candidate"]
    pillar = inputs["pillar"]

    # STEP 1: fulltext_search bypassing INV-04 — a contradiction scan
    # must see every existing node regardless of confidence tier.
    from aether.models.enums import PillarName

    candidates = await fulltext_search(
        candidate["title"], PillarName(pillar), db, limit=10, bypass_inv04=True
    )

    conflicts: list[ConflictPair] = []
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    system_prompt = _PROMPT_PATH.read_text()

    for existing in candidates:
        similarity = _approx_similarity(candidate["title"], existing.title)
        if similarity <= _SIMILARITY_THRESHOLD:
            continue

        comparison = await _compare_via_llm(
            client, system_prompt, candidate["content"], existing.content, existing.id
        )
        if comparison is None:
            logger.warning(
                "contradiction_detector: skipping comparison after LLM failure",
                extra={"existing_id": str(existing.id)},
            )
            continue
        if comparison.get("conflict") == "yes":
            conflicts.append(
                ConflictPair(
                    node_id=existing.id,
                    existing_title=existing.title,
                    conflict_description=comparison.get("description", ""),
                    conflict_severity=comparison.get("severity", "partial"),
                )
            )

    return ConflictResult(conflicts=conflicts).model_dump(mode="json")


def _approx_similarity(a: str, b: str) -> float:
    """
    Cheap pre-filter similarity score (not the LLM call itself) used
    only to decide which fulltext-search hits are worth an LLM
    comparison at all. Section 15 says "results with similarity score
    > 0.5" without specifying how that score is computed prior to the
    LLM call; Jaccard token overlap is used here for the same reason
    documented in write_protocol.py's supersession logic (GAP-03
    Option B) — consistent, cheap, and already implemented elsewhere
    in this codebase.
    """
    ta, tb = set(a.lower().split()), set(b.lower().split())
    if not ta and not tb:
        return 1.0
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


async def _compare_via_llm(client, system_prompt, candidate_content, existing_content, existing_id):
    """One LLM comparison call. Returns None on timeout or invalid JSON — never raises."""
    user_message = json.dumps(
        {
            "candidate_content": candidate_content,
            "existing_content": existing_content,
            "existing_id": str(existing_id),
        }
    )
    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=256,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except APITimeoutError:
        return None
    except Exception:
        logger.exception("contradiction_detector: LLM call failed")
        return None

    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
