"""
aether.skills.cognitive.intent_parser
========================================

SKILL-01 (Phase-0 Prompt Section 15). Converts raw user input into a
structured ``UserIntent`` via an LLM call. Never writes to the DB,
never calls the Action Gateway — ``raw_input`` is treated strictly as
data, never as instructions (RISK-S01's prompt-injection concern
applies here directly).

Forward-reference note: the system prompt is read from
``agents/prompts/intent_extraction.txt`` (Step 18), which does not
exist yet at Step 15. Reading it is deferred to call time (a file
read, not a Python import), so this module loads fine now; it will
raise ``FileNotFoundError`` if actually invoked before Step 18. Tested
here with the file-read mocked, same approach used for the other
forward-referenced dependencies in the memory-layer checkpoint.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic, APITimeoutError

from aether.config import settings
from aether.schemas.agent import UserIntent

logger = logging.getLogger("aether.skills.cognitive.intent_parser")

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "intent_extraction.txt"
_MAX_INPUT_CHARS = 4000


async def parse_intent(inputs: dict, db) -> dict:
    """
    inputs: ``{'raw_input': str}``. Returns a ``UserIntent`` dict.
    Never raises on LLM failure — always returns a (possibly degraded)
    ``UserIntent``, per FM-01/FM-02's "Do NOT raise" instruction.
    """
    raw_input = inputs["raw_input"]
    truncated = False
    if len(raw_input) > _MAX_INPUT_CHARS:
        raw_input = raw_input[:_MAX_INPUT_CHARS]
        truncated = True

    system_prompt = _PROMPT_PATH.read_text()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_message = f"Parse this input into a UserIntent JSON: {raw_input}"

    parsed = await _call_and_parse(client, system_prompt, user_message)
    if parsed is None:
        # Retry once with an explicit schema reminder (FM-02).
        reminder = (
            f"{user_message}\n\nRespond with ONLY a JSON object matching this schema: "
            '{"action_type": "query|write|review|task|synthesize|clarify", "subject": str, '
            '"implied_pillars": [str], "urgency": "immediate|standard|low", '
            '"time_horizon": str|null, "entities": [str]}'
        )
        parsed = await _call_and_parse(client, system_prompt, reminder)

    if parsed is None:
        # Second failure: degrade, never raise (FM-01/FM-02).
        return UserIntent(
            raw_input=raw_input,
            action_type="clarify",
            subject=raw_input[:100] + (" [truncated]" if truncated else ""),
            implied_pillars=["legal"],  # FM-03 default
            urgency="standard",
            ambiguity_flag=True,
            clarification="Could not parse. Please rephrase.",
        ).model_dump(mode="json")

    # FM-03: empty implied_pillars defaults to ['legal'] with ambiguity flagged.
    implied_pillars = parsed.get("implied_pillars") or []
    # Bug fixed here (found via testing the ambiguity-path branch of
    # MasterAgent.process()): the LLM's own ambiguity_flag/clarification
    # must be respected, not unconditionally overwritten. FM-03's
    # empty-pillars condition additionally *forces* ambiguity_flag to
    # True (and supplies a default pillar) on top of whatever the LLM
    # said — it does not replace the LLM's own judgment when pillars
    # were provided.
    ambiguity_flag = bool(parsed.get("ambiguity_flag", False))
    if not implied_pillars:
        implied_pillars = ["legal"]
        ambiguity_flag = True

    intent = UserIntent(
        raw_input=raw_input,
        action_type=parsed.get("action_type", "clarify"),
        subject=parsed.get("subject", raw_input[:100]),
        implied_pillars=implied_pillars,
        urgency=parsed.get("urgency", "standard"),
        time_horizon=parsed.get("time_horizon"),
        entities=parsed.get("entities") or [],
        ambiguity_flag=ambiguity_flag,
        clarification=parsed.get("clarification"),
    )
    return intent.model_dump(mode="json")


async def _call_and_parse(client: AsyncAnthropic, system_prompt: str, user_message: str) -> dict | None:
    """
    One LLM call attempt. Returns the parsed dict, or ``None`` on
    timeout or invalid JSON (FM-01/FM-02) — never raises.
    """
    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=512,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except APITimeoutError:
        logger.warning("intent_parser: LLM call timed out")
        return None
    except Exception:
        logger.exception("intent_parser: LLM call failed")
        return None

    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("intent_parser: LLM returned non-JSON response")
        return None
