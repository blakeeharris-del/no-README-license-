"""
aether.skills.registry
=========================

``SKILL_REGISTRY`` and ``SKILL_TIMEOUTS`` (Phase-0 Prompt Section 14).

Imported lazily by ``invoke_skill()`` (inside the function body, not
at module import time) specifically to avoid a circular import:
``write_protocol.py`` -> (deferred) ``invoker.py`` -> this module ->
every skill module -> some of which (``node_writer.py``) import back
into ``aether.memory.write_protocol``.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from aether.skills.cognitive.contradiction_detector import detect_contradiction
from aether.skills.cognitive.intent_parser import parse_intent
from aether.skills.cognitive.signal_scorer import score_signal
from aether.skills.evaluative.output_validator import validate_output
from aether.skills.operational.action_gateway import action_gateway_skill
from aether.skills.operational.context_assembler import assemble_context
from aether.skills.operational.node_writer import write_node_skill
from aether.skills.operational.session_initializer import initialize_session
from aether.skills.safety.authority_checker import check_authority_skill

SkillFn = Callable[[dict, object], Awaitable[dict]]

SKILL_REGISTRY: dict[str, SkillFn] = {
    "cognitive.intent_parser": parse_intent,
    "cognitive.contradiction_detector": detect_contradiction,
    "cognitive.signal_scorer": score_signal,
    "operational.node_writer": write_node_skill,
    "operational.context_assembler": assemble_context,
    "operational.session_initializer": initialize_session,
    "operational.action_gateway": action_gateway_skill,
    "evaluative.output_validator": validate_output,
    "safety.authority_checker": check_authority_skill,
}

SKILL_TIMEOUTS: dict[str, int] = {  # milliseconds
    "cognitive.intent_parser": 10000,
    "cognitive.contradiction_detector": 15000,
    "cognitive.signal_scorer": 5000,
    "operational.node_writer": 10000,
    "operational.context_assembler": 10000,
    "operational.session_initializer": 15000,
    "operational.action_gateway": 10000,
    "evaluative.output_validator": 5000,
    "safety.authority_checker": 2000,
}
