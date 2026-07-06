"""
aether.skills._llm
====================

Shared LLM-call helper for Phase-1 skills.

The Phase-0 LLM skills (intent_parser, contradiction_detector) each
carry their own inline ``_call_and_parse`` with identical
timeout/JSON-retry/degrade logic. Rather than copy that a dozen more
times across the Phase-1 analytical/executive/cognitive skills, they
share this one helper. Phase-0's skills are intentionally left as-is
(no reason to churn working, tested code mid-phase).

Layering: this module imports only ``aether.config`` and ``anthropic``
— it never reaches into memory/, models/, or agents/, so it does not
violate the skills-layer import rule in CLAUDE.md.

Testing hook: skills that use this helper are mocked by patching
``aether.skills._llm.AsyncAnthropic`` (see the ``mock_llm_client``
fixture) — a single patch point for every skill built on top of it.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from anthropic import AsyncAnthropic, APITimeoutError

from aether.config import settings


def _extract_text(response) -> str:
    return "".join(block.text for block in response.content if block.type == "text")


def _loads(text: str) -> Optional[dict]:
    # Tolerate a stray ```json fence if the model adds one, then parse.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if "```" in cleaned[3:] else cleaned
        cleaned = cleaned.removeprefix("json").strip().strip("`").strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


async def call_json(
    system: str,
    user: str,
    *,
    logger: logging.Logger,
    max_tokens: int = 1024,
    temperature: float = 0.3,
    retries: int = 1,
) -> Optional[dict]:
    """
    Make an LLM call expected to return a single JSON object.

    Returns the parsed dict, or ``None`` after ``retries + 1`` attempts
    fail (timeout / non-JSON / non-object). Never raises on LLM error —
    callers degrade gracefully per each skill's Failure Modes, matching
    the Phase-0 "Do NOT raise" convention.
    """
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    attempt = 0
    while attempt <= retries:
        attempt += 1
        try:
            response = await client.messages.create(
                model=settings.anthropic_model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except APITimeoutError:
            logger.warning("%s: LLM call timed out (attempt %d)", logger.name, attempt)
            continue
        except Exception:
            logger.exception("%s: LLM call failed (attempt %d)", logger.name, attempt)
            continue

        parsed = _loads(_extract_text(response))
        if parsed is not None:
            return parsed
        logger.warning("%s: LLM returned non-JSON/object (attempt %d)", logger.name, attempt)

    return None
