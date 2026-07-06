"""
aether.agents.sub_agents.catalog
==================================

The single source of truth for the 30-sub-agent roster
(AGENT_ARCHITECTURE §5-6, Missing Specs Volume 2 SA-01..SA-30). Mirrors
the ``sub_agents`` table (migration 0005) and seeds it, exactly as
``skills.catalog`` does for the ``skills`` table.

parent_agent is the pillar name (or "master" for the three orchestrator
sub-agents), matching Missing Specs' seed example. authority_level maps
L0->0, L1->1.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    parent_agent: str
    domain: str
    trigger_event: str
    termination_condition: str
    max_duration_ms: int
    authority_level: int
    description: str
    max_iterations: int = 1
    phase_introduced: int = 1


def _s(name, parent, domain, trigger, term, dur, auth, desc):
    return SubAgentSpec(name, parent, domain, trigger, term, dur, auth, desc)


SUB_AGENT_CATALOG: dict[str, SubAgentSpec] = {
    s.name: s for s in [
        # ---- Legal (5) --------------------------------------------------
        _s("legal.deadline_scanner", "legal", "legal", "session.opened",
           "Returns deadline list to parent", 30000, 0,
           "Query legal nodes for deadlines in next 90 days; score; escalate P0/P1."),
        _s("legal.contract_reviewer", "legal", "legal", "node.written:artifact",
           "Returns write_proposals to parent", 45000, 1,
           "Parse a contract artifact node; extract parties/dates/obligations."),
        _s("legal.entity_mapper", "legal", "legal", "manual",
           "Returns entity map", 20000, 0,
           "Build entity graph from legal entity nodes and links."),
        _s("legal.obligation_tracker", "legal", "legal", "session.opened",
           "Returns obligation list", 20000, 0,
           "Daily scan for legal obligations approaching deadline."),
        _s("legal.regulatory_compliance_scanner", "legal", "legal", "session.opened",
           "Returns compliance item list", 20000, 0,
           "Identify regulatory compliance items with approaching deadlines."),
        # ---- Finance (6) ------------------------------------------------
        _s("finance.net_worth_calculator", "finance", "personal_finance", "manual",
           "Returns net worth snapshot", 15000, 0,
           "Compute dated net worth snapshot from finance nodes."),
        _s("finance.cash_flow_monitor", "finance", "personal_finance", "session.opened",
           "Returns cash flow summary", 15000, 0,
           "Summarize income vs expense; flag anomalies; upcoming bills."),
        _s("finance.deadline_scanner", "finance", "personal_finance", "session.opened",
           "Returns deadline list", 20000, 0,
           "Scan finance nodes for tax/payment/insurance deadlines."),
        _s("finance.projection_builder", "finance", "personal_finance", "manual",
           "Returns projection (speculative)", 30000, 1,
           "Build financial projection; always speculative with assumptions."),
        _s("finance.tax_deadline_scanner", "finance", "personal_finance", "session.opened",
           "Returns tax deadline list", 20000, 0,
           "Surface tax filing deadlines and estimated payment dates."),
        _s("finance.insurance_expiry_scanner", "finance", "personal_finance", "session.opened",
           "Returns policy expiry list", 20000, 0,
           "Surface insurance policies approaching renewal or expiry."),
        # ---- Career (4) -------------------------------------------------
        _s("career.credential_tracker", "career", "career", "session.opened",
           "Returns credential list", 15000, 0,
           "Scan career nodes for credentials with renewal dates; escalate <60d."),
        _s("career.opportunity_ranker", "career", "career", "manual",
           "Returns ranked opportunities", 20000, 1,
           "Rank active opportunities by fit/urgency/alignment with rationale."),
        _s("career.trajectory_assessor", "career", "career", "weekly_review",
           "Returns trajectory (inferred)", 20000, 1,
           "Assess career trajectory from role/credential nodes; always inferred."),
        _s("career.skill_gap_identifier", "career", "career", "manual",
           "Returns skill gap analysis", 20000, 1,
           "Compare credentials vs opportunity requirements; inferred gaps."),
        # ---- Business (4) -----------------------------------------------
        _s("business.pipeline_monitor", "business", "business", "session.opened",
           "Returns pipeline summary", 20000, 0,
           "Summarize client pipeline (active/at-risk/closing); revenue forecast."),
        _s("business.obligation_tracker", "business", "business", "session.opened",
           "Returns obligation list", 15000, 0,
           "Scan business obligation nodes for deadlines; escalate P0/P1."),
        _s("business.health_scorecard", "business", "business", "weekly_review",
           "Returns health scorecard", 20000, 1,
           "Produce business health scorecard for weekly review."),
        _s("business.vendor_obligation_tracker", "business", "business", "session.opened",
           "Returns vendor obligation list", 20000, 0,
           "Track vendor contract renewals and payment obligations."),
        # ---- Health (4) -------------------------------------------------
        _s("health.medication_monitor", "health", "health", "session.opened",
           "Returns medication/appointment alerts", 15000, 0,
           "Check medication refills and appointment proximity; P0/P1 alerts."),
        _s("health.pattern_detector", "health", "health", "synthesis.completed",
           "Returns health patterns", 20000, 1,
           "Detect health patterns from lab/habit nodes; conservative."),
        _s("health.provider_mapper", "health", "health", "manual",
           "Returns provider map", 15000, 0,
           "Map providers by specialty, last visit, next appointment."),
        _s("health.appointment_reminder", "health", "health", "session.opened",
           "Returns upcoming appointments", 15000, 0,
           "Surface medical appointments within 7 days; P0<24h, P1<3d."),
        # ---- Relationships (4) ------------------------------------------
        _s("relationships.commitment_tracker", "relationships", "relationships", "session.opened",
           "Returns open commitments", 15000, 0,
           "Scan relationships pillar for open commitments; flag overdue."),
        _s("relationships.contact_cadence", "relationships", "relationships", "weekly_review",
           "Returns contacts due", 15000, 0,
           "Identify people due for contact by tier cadence rules."),
        _s("relationships.learning_progress", "relationships", "relationships", "weekly_review",
           "Returns learning items", 15000, 0,
           "Summarize active learning items with progress and alignment."),
        _s("relationships.key_date_reminder", "relationships", "relationships", "session.opened",
           "Returns key dates", 15000, 0,
           "Surface important dates (birthdays, anniversaries) within 14 days."),
        # ---- Orchestrator (3) — report to master ------------------------
        _s("orchestrator.multi_pillar_collector", "master", "cross_pillar", "orchestrated",
           "Returns merged node set", 30000, 0,
           "Query 2+ pillars, merge/dedupe, INV-04 filter, token budget."),
        _s("orchestrator.decision_assembler", "master", "cross_pillar", "challenge_and_prepare",
           "Returns decision brief", 45000, 1,
           "Assemble full decision brief across affected pillars."),
        _s("orchestrator.synthesis_coordinator", "master", "cross_pillar", "session.closed",
           "Returns diff report or skip", 300000, 1,
           "Coordinate synthesis cycle; manage advisory lock; present diff. Foundation §10.4."),
    ]
}

assert len(SUB_AGENT_CATALOG) == 30, f"expected 30 sub-agents, found {len(SUB_AGENT_CATALOG)}"


async def seed_sub_agent_catalog(db: AsyncSession, *, commit: bool = True) -> int:
    """Idempotently upsert all 30 sub-agents into the ``sub_agents`` table.

    Upsert key is ``name`` (UNIQUE). Runs as the app role, which holds
    SELECT/INSERT/UPDATE on sub_agents (migration 0005).
    """
    for spec in SUB_AGENT_CATALOG.values():
        await db.execute(
            text(
                """
                INSERT INTO sub_agents
                    (name, parent_agent, domain, description, trigger_event,
                     termination_condition, max_duration_ms, max_iterations,
                     authority_level, phase_introduced, status)
                VALUES (:name, :parent, :domain, :desc, :trigger, :term,
                        :dur, :iters, :auth, :phase, 'active')
                ON CONFLICT (name) DO UPDATE SET
                    parent_agent = EXCLUDED.parent_agent,
                    domain = EXCLUDED.domain,
                    description = EXCLUDED.description,
                    trigger_event = EXCLUDED.trigger_event,
                    termination_condition = EXCLUDED.termination_condition,
                    max_duration_ms = EXCLUDED.max_duration_ms,
                    max_iterations = EXCLUDED.max_iterations,
                    authority_level = EXCLUDED.authority_level,
                    status = 'active'
                """
            ),
            {"name": spec.name, "parent": spec.parent_agent, "domain": spec.domain,
             "desc": spec.description, "trigger": spec.trigger_event,
             "term": spec.termination_condition, "dur": spec.max_duration_ms,
             "iters": spec.max_iterations, "auth": spec.authority_level,
             "phase": spec.phase_introduced},
        )
    if commit:
        await db.commit()
    return len(SUB_AGENT_CATALOG)
