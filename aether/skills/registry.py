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

from aether.skills.analytical.business_health import assess_business_health
from aether.skills.analytical.career_trajectory import analyze_career_trajectory
from aether.skills.analytical.financial_net_worth import compute_net_worth
from aether.skills.analytical.health_pattern_detector import detect_health_patterns
from aether.skills.analytical.legal_deadline_surfacer import surface_legal_deadlines
from aether.skills.analytical.relationship_graph import build_relationship_graph
from aether.skills.cognitive.confidence_scorer import score_confidence
from aether.skills.cognitive.contradiction_detector import detect_contradiction
from aether.skills.cognitive.cross_pillar_connector import connect_cross_pillar
from aether.skills.cognitive.decision_framer import frame_decision
from aether.skills.cognitive.intent_parser import parse_intent
from aether.skills.cognitive.signal_scorer import score_signal
from aether.skills.cognitive.synthesis_engine import run_synthesis_engine
from aether.skills.evaluative.confidence_auditor import audit_confidence
from aether.skills.evaluative.loop_health_checker import check_loop_health
from aether.skills.evaluative.memory_integrity_checker import check_memory_integrity
from aether.skills.evaluative.output_validator import validate_output
from aether.skills.evaluative.skill_performance_tracker import track_skill_performance
from aether.skills.executive.approval_presenter import present_approval
from aether.skills.executive.decision_protocol import run_decision_protocol
from aether.skills.executive.session_briefer import build_session_brief
from aether.skills.executive.synthesis_diff_presenter import present_synthesis_diff
from aether.skills.executive.weekly_reviewer import build_weekly_review
from aether.skills.operational.action_gateway import action_gateway_skill
from aether.skills.operational.context_assembler import assemble_context
from aether.skills.operational.deadline_monitor import monitor_deadlines
from aether.skills.operational.node_linker import link_nodes
from aether.skills.operational.node_writer import write_node_skill
from aether.skills.operational.session_initializer import initialize_session
from aether.skills.safety.authority_checker import check_authority_skill

SkillFn = Callable[[dict, object], Awaitable[dict]]

SKILL_REGISTRY: dict[str, SkillFn] = {
    "cognitive.intent_parser": parse_intent,
    "cognitive.contradiction_detector": detect_contradiction,
    "cognitive.signal_scorer": score_signal,
    "cognitive.confidence_scorer": score_confidence,
    "cognitive.synthesis_engine": run_synthesis_engine,
    "cognitive.decision_framer": frame_decision,
    "cognitive.cross_pillar_connector": connect_cross_pillar,
    "analytical.legal_deadline_surfacer": surface_legal_deadlines,
    "analytical.financial_net_worth": compute_net_worth,
    "analytical.career_trajectory": analyze_career_trajectory,
    "analytical.business_health": assess_business_health,
    "analytical.health_pattern_detector": detect_health_patterns,
    "analytical.relationship_graph": build_relationship_graph,
    "operational.node_writer": write_node_skill,
    "operational.context_assembler": assemble_context,
    "operational.node_linker": link_nodes,
    "operational.deadline_monitor": monitor_deadlines,
    "operational.session_initializer": initialize_session,
    "operational.action_gateway": action_gateway_skill,
    "executive.session_briefer": build_session_brief,
    "executive.weekly_reviewer": build_weekly_review,
    "executive.decision_protocol": run_decision_protocol,
    "executive.approval_presenter": present_approval,
    "executive.synthesis_diff_presenter": present_synthesis_diff,
    "evaluative.output_validator": validate_output,
    "evaluative.confidence_auditor": audit_confidence,
    "evaluative.skill_performance_tracker": track_skill_performance,
    "evaluative.loop_health_checker": check_loop_health,
    "evaluative.memory_integrity_checker": check_memory_integrity,
    "safety.authority_checker": check_authority_skill,
}

SKILL_TIMEOUTS: dict[str, int] = {  # milliseconds
    "cognitive.intent_parser": 10000,
    "cognitive.contradiction_detector": 15000,
    "cognitive.signal_scorer": 5000,
    "cognitive.confidence_scorer": 5000,
    "cognitive.synthesis_engine": 120000,
    "cognitive.decision_framer": 20000,
    "cognitive.cross_pillar_connector": 20000,
    "analytical.legal_deadline_surfacer": 10000,
    "analytical.financial_net_worth": 15000,
    "analytical.career_trajectory": 15000,
    "analytical.business_health": 15000,
    "analytical.health_pattern_detector": 15000,
    "analytical.relationship_graph": 10000,
    "operational.node_writer": 10000,
    "operational.context_assembler": 10000,
    "operational.node_linker": 5000,
    "operational.deadline_monitor": 10000,
    "operational.session_initializer": 15000,
    "operational.action_gateway": 10000,
    "executive.session_briefer": 15000,
    "executive.weekly_reviewer": 30000,
    "executive.decision_protocol": 30000,
    "executive.approval_presenter": 5000,
    "executive.synthesis_diff_presenter": 10000,
    "evaluative.output_validator": 5000,
    "evaluative.confidence_auditor": 15000,
    "evaluative.skill_performance_tracker": 10000,
    "evaluative.loop_health_checker": 30000,
    "evaluative.memory_integrity_checker": 20000,
    "safety.authority_checker": 2000,
}
