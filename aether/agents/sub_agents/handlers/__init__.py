"""
aether.agents.sub_agents.handlers
===================================

Registry mapping each of the 30 sub-agent names to its handler
function. run_sub_agent() dispatches through SUB_AGENT_HANDLERS.
"""

from __future__ import annotations

from aether.agents.sub_agents.handlers import (
    career_business,
    finance,
    health_relationships as hr,
    legal,
    orchestrator,
)

SUB_AGENT_HANDLERS = {
    # Legal (5)
    "legal.deadline_scanner": legal.deadline_scanner,
    "legal.contract_reviewer": legal.contract_reviewer,
    "legal.entity_mapper": legal.entity_mapper,
    "legal.obligation_tracker": legal.obligation_tracker,
    "legal.regulatory_compliance_scanner": legal.regulatory_compliance_scanner,
    # Finance (6)
    "finance.net_worth_calculator": finance.net_worth_calculator,
    "finance.cash_flow_monitor": finance.cash_flow_monitor,
    "finance.deadline_scanner": finance.deadline_scanner,
    "finance.projection_builder": finance.projection_builder,
    "finance.tax_deadline_scanner": finance.tax_deadline_scanner,
    "finance.insurance_expiry_scanner": finance.insurance_expiry_scanner,
    # Career (4)
    "career.credential_tracker": career_business.credential_tracker,
    "career.opportunity_ranker": career_business.opportunity_ranker,
    "career.trajectory_assessor": career_business.trajectory_assessor,
    "career.skill_gap_identifier": career_business.skill_gap_identifier,
    # Business (4)
    "business.pipeline_monitor": career_business.pipeline_monitor,
    "business.obligation_tracker": career_business.obligation_tracker,
    "business.health_scorecard": career_business.health_scorecard,
    "business.vendor_obligation_tracker": career_business.vendor_obligation_tracker,
    # Health (4)
    "health.medication_monitor": hr.medication_monitor,
    "health.pattern_detector": hr.pattern_detector,
    "health.provider_mapper": hr.provider_mapper,
    "health.appointment_reminder": hr.appointment_reminder,
    # Relationships (4)
    "relationships.commitment_tracker": hr.commitment_tracker,
    "relationships.contact_cadence": hr.contact_cadence,
    "relationships.learning_progress": hr.learning_progress,
    "relationships.key_date_reminder": hr.key_date_reminder,
    # Orchestrator (3)
    "orchestrator.multi_pillar_collector": orchestrator.multi_pillar_collector,
    "orchestrator.decision_assembler": orchestrator.decision_assembler,
    "orchestrator.synthesis_coordinator": orchestrator.synthesis_coordinator,
}

assert len(SUB_AGENT_HANDLERS) == 30, f"expected 30 handlers, found {len(SUB_AGENT_HANDLERS)}"
