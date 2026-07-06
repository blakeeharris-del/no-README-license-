"""
aether.agents.specialists
============================

The six Specialist Agents (one per pillar). Per AGENT_ARCHITECTURE §5 /
Foundation §10.2 and the architecture principles:

  * thin — they direct their pillar's sub-agents and aggregate output
    (principle: specialists reason, they don't store);
  * stateless between invocations (principle 6) — all state arrives in
    the context packet, nothing is kept on the instance;
  * they route ONLY to their own sub-agents (EC-16) — a request for a
    sub-agent they don't own is ignored;
  * they NEVER produce user-facing output (EC-16 / principle 2) — every
    result is a structured envelope returned to the Master Agent, which
    is the sole point of contact with the user.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aether.agents.sub_agents.runtime import run_sub_agent
from aether.models.enums import AgentName, PillarName

logger = logging.getLogger("aether.agents.specialists")


class SpecialistAgent:
    """Base class for the six pillar specialists. Stateless."""

    pillar: PillarName
    agent_name: AgentName
    sub_agents: tuple[str, ...] = ()

    async def handle(
        self,
        context_packet: dict,
        db: AsyncSession,
        *,
        requested: list[str] | None = None,
    ) -> dict:
        """
        Run this specialist's sub-agents (all by default, or a requested
        subset restricted to those it owns) and aggregate their results.
        Returns a structured envelope — never user-facing text.
        """
        session_id: UUID = context_packet["session_id"]
        sub_inputs = context_packet.get("sub_agent_inputs", {}) or {}

        # EC-16: only ever this specialist's own sub-agents.
        if requested is None:
            targets = list(self.sub_agents)
        else:
            targets = [name for name in requested if name in self.sub_agents]
        rejected = [name for name in (requested or []) if name not in self.sub_agents]
        if rejected:
            logger.info("%s rejected out-of-scope sub-agents: %s", self.agent_name.value, rejected)

        results: dict[str, dict] = {}
        for name in targets:
            inputs = {"session_id": str(session_id), **sub_inputs.get(name, {})}
            res = await run_sub_agent(name, inputs, session_id, db)
            results[name] = {"status": res.status, "run_id": str(res.run_id), "output": res.output}

        # Aggregate: surface how many escalation-worthy items were found,
        # for the Master to weave into a single user-facing reply.
        return {
            "agent": self.agent_name.value,
            "pillar": self.pillar.value,
            "sub_agent_results": results,
            "sub_agents_run": targets,
            "sub_agents_rejected": rejected,
            "user_facing": False,  # EC-16: output flows through the Master Agent
        }


class LegalAgent(SpecialistAgent):
    pillar = PillarName.LEGAL
    agent_name = AgentName.LEGAL
    sub_agents = (
        "legal.deadline_scanner", "legal.contract_reviewer", "legal.entity_mapper",
        "legal.obligation_tracker", "legal.regulatory_compliance_scanner",
    )


class FinanceAgent(SpecialistAgent):
    pillar = PillarName.PERSONAL_FINANCE
    agent_name = AgentName.FINANCE
    sub_agents = (
        "finance.net_worth_calculator", "finance.cash_flow_monitor", "finance.deadline_scanner",
        "finance.projection_builder", "finance.tax_deadline_scanner",
        "finance.insurance_expiry_scanner",
    )


class CareerAgent(SpecialistAgent):
    pillar = PillarName.CAREER
    agent_name = AgentName.CAREER
    sub_agents = (
        "career.credential_tracker", "career.opportunity_ranker",
        "career.trajectory_assessor", "career.skill_gap_identifier",
    )


class BusinessAgent(SpecialistAgent):
    pillar = PillarName.BUSINESS
    agent_name = AgentName.BUSINESS
    sub_agents = (
        "business.pipeline_monitor", "business.obligation_tracker",
        "business.health_scorecard", "business.vendor_obligation_tracker",
    )


class HealthAgent(SpecialistAgent):
    pillar = PillarName.HEALTH
    agent_name = AgentName.HEALTH
    sub_agents = (
        "health.medication_monitor", "health.pattern_detector",
        "health.provider_mapper", "health.appointment_reminder",
    )


class RelationshipsAgent(SpecialistAgent):
    pillar = PillarName.RELATIONSHIPS
    agent_name = AgentName.RELATIONSHIPS
    sub_agents = (
        "relationships.commitment_tracker", "relationships.contact_cadence",
        "relationships.learning_progress", "relationships.key_date_reminder",
    )


# Master-agent-facing registry: pillar -> specialist instance (stateless,
# so a single shared instance per pillar is fine).
SPECIALIST_AGENTS: dict[PillarName, SpecialistAgent] = {
    PillarName.LEGAL: LegalAgent(),
    PillarName.PERSONAL_FINANCE: FinanceAgent(),
    PillarName.CAREER: CareerAgent(),
    PillarName.BUSINESS: BusinessAgent(),
    PillarName.HEALTH: HealthAgent(),
    PillarName.RELATIONSHIPS: RelationshipsAgent(),
}
