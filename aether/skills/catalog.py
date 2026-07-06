"""
aether.skills.catalog
=======================

The single source of truth for the **34-skill catalog** (Implementation
Plan §9; the "34 not 30" resolution recorded in AETHER_PHASE1_PROMPT
§0 — the six category headers total 7+6+6+5+5+5 = 34, and every site
that says "30" is stale).

Phase-0 ran entirely off the in-code ``SKILL_REGISTRY`` dict and never
populated the ``skills`` table, so the skill *lifecycle* column
(Draft→Review→Validated→Staged→Active→Deprecated→Archived,
Foundation §10.7.1 / Impl Plan §8.3) had no rows at all. EC-19 requires
every catalog skill to be **Active**, and Foundation §10.7.1 states
that "no skill may be invoked unless it is Active". This module closes
that gap:

  * ``SKILL_CATALOG`` describes all 34 skills (category, version,
    timeout, authority, phase, purpose) from Impl Plan §9.
  * ``seed_skill_catalog()`` idempotently upserts one row per skill
    into the ``skills`` table, setting status='active' for skills that
    are *genuinely invocable right now* and status='draft' for skills
    not yet implemented. This keeps the lifecycle honest: a row is
    Active if and only if the code behind it actually runs.
  * ``invoke_skill()`` gates on that Active status (see invoker.py).

"Genuinely invocable" is derived, not hand-maintained:
  active(name) := name in SKILL_REGISTRY  (registry-dispatched skills)
                  OR name in _DIRECTLY_INVOKED_ACTIVE  (safety skills
                  that bypass the invoke_skill wrapper but are
                  implemented and called directly).
As each Phase-1 skill is implemented and added to SKILL_REGISTRY (or,
for a safety skill, to _DIRECTLY_INVOKED_ACTIVE), re-running the seed
flips it Draft→Active automatically — no second edit site to forget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.enums import SkillCategory


@dataclass(frozen=True)
class SkillSpec:
    """Catalog metadata for one skill (Impl Plan §9)."""

    name: str
    category: SkillCategory
    timeout_ms: int
    authority: str          # "L0".."L3" | "SYSTEM" — metadata (no skills-table column)
    phase: int              # catalog Phase tag (0/1/2). See §0 discrepancy notes.
    purpose: str
    version: str = "1.0.0"


# Authority sentinels (Foundation §9.1 L0-L5 + SYSTEM for safety skills).
_L0, _L1, _L2, _L3, _SYS = "L0", "L1", "L2", "L3", "SYSTEM"


# ---------------------------------------------------------------------------
# All 34 skills, grouped by the six categories of Impl Plan §9.
# Phase tags are the catalog's own; the four that Step-0's inventory found
# stale/conflicting are annotated inline and were resolved per the governance
# precedence recorded in the Phase-1 HANDOFF:
#   operational.node_linker   — catalog says P0, was never built  -> build P1
#   executive.approval_presenter — catalog says P0, absent        -> build P1
#   executive.decision_protocol  — catalog P0, placeholder only    -> build P1
#   evaluative.loop_health_checker — catalog P2                    -> build P1 (active
#                                    + one scorecard; full stability stays P2)
# ---------------------------------------------------------------------------
SKILL_CATALOG: dict[str, SkillSpec] = {
    s.name: s
    for s in [
        # -- Category 1: Cognitive (7) ------------------------------------
        SkillSpec("cognitive.intent_parser", SkillCategory.COGNITIVE, 10_000, _L1, 0,
                  "Convert raw user input to structured UserIntent."),
        SkillSpec("cognitive.contradiction_detector", SkillCategory.COGNITIVE, 15_000, _L1, 0,
                  "Detect semantic conflicts between a candidate node and existing L2/L3 nodes."),
        SkillSpec("cognitive.confidence_scorer", SkillCategory.COGNITIVE, 5_000, _L1, 1,
                  "Evaluate a node's evidentiary basis; rule-based; returns explicit/inferred/speculative."),
        SkillSpec("cognitive.synthesis_engine", SkillCategory.COGNITIVE, 120_000, _L1, 1,
                  "From L2 nodes for a pillar, produce candidate L3 belief nodes."),
        SkillSpec("cognitive.decision_framer", SkillCategory.COGNITIVE, 20_000, _L1, 1,
                  "Structure a proposed action into options, assumptions, tradeoffs, risks."),
        SkillSpec("cognitive.cross_pillar_connector", SkillCategory.COGNITIVE, 20_000, _L1, 1,
                  "Identify meaningful connections between nodes across pillar boundaries."),
        SkillSpec("cognitive.signal_scorer", SkillCategory.COGNITIVE, 5_000, _L0, 0,
                  "Score a signal on Impact/Time-Sensitivity/Confidence/Noise; rule-based."),
        # -- Category 2: Analytical (6) -----------------------------------
        SkillSpec("analytical.legal_deadline_surfacer", SkillCategory.ANALYTICAL, 10_000, _L0, 1,
                  "Query legal nodes for deadlines within window; score; escalate P0/P1."),
        SkillSpec("analytical.financial_net_worth", SkillCategory.ANALYTICAL, 15_000, _L0, 1,
                  "Compute net worth from explicit asset/liability nodes; dated snapshot."),
        SkillSpec("analytical.career_trajectory", SkillCategory.ANALYTICAL, 15_000, _L1, 1,
                  "Infer trajectory from role/credential nodes; output labeled 'inferred'."),
        SkillSpec("analytical.business_health", SkillCategory.ANALYTICAL, 15_000, _L1, 1,
                  "Summarize revenue, clients, obligations, strategy; health scorecard."),
        SkillSpec("analytical.health_pattern_detector", SkillCategory.ANALYTICAL, 15_000, _L1, 1,
                  "Identify trends in lab/habit/medication nodes; conservative; always disclaims."),
        SkillSpec("analytical.relationship_graph", SkillCategory.ANALYTICAL, 10_000, _L0, 1,
                  "Map people, commitments, last-contact dates; tiered relationship snapshot."),
        # -- Category 3: Operational (6) ----------------------------------
        SkillSpec("operational.node_writer", SkillCategory.OPERATIONAL, 10_000, _L2, 0,
                  "Wrap write_node() 10-step protocol; requires L2+; enforces INV-01/02/03/07."),
        SkillSpec("operational.context_assembler", SkillCategory.OPERATIONAL, 10_000, _L1, 0,
                  "Build complete 6-section context packet; enforces INV-04 filter + token budget."),
        SkillSpec("operational.node_linker", SkillCategory.OPERATIONAL, 5_000, _L2, 0,
                  "Create typed directed node_links row; triggers contradiction_enforcer on 'contradicts'."),
        SkillSpec("operational.deadline_monitor", SkillCategory.OPERATIONAL, 10_000, _L0, 1,
                  "Scan all active nodes with deadline metadata; score and escalate P0/P1."),
        SkillSpec("operational.session_initializer", SkillCategory.OPERATIONAL, 15_000, _L0, 0,
                  "Rebuild L1 Working Memory from L2/L3; handles new/cold-start/crash-recovery."),
        SkillSpec("operational.action_gateway", SkillCategory.OPERATIONAL, 10_000, _L3, 0,
                  "Single enforcement point for external actions; Phase-0 stub; enforces INV-05/10."),
        # -- Category 4: Executive (5) ------------------------------------
        SkillSpec("executive.session_briefer", SkillCategory.EXECUTIVE, 15_000, _L1, 1,
                  "Produce session brief: priorities, P0/P1 items, deadlines, pending approvals, diffs."),
        SkillSpec("executive.weekly_reviewer", SkillCategory.EXECUTIVE, 30_000, _L1, 1,
                  "Produce weekly review: life position across pillars, patterns, open decisions."),
        SkillSpec("executive.decision_protocol", SkillCategory.EXECUTIVE, 30_000, _L1, 0,
                  "Assemble structured decision package (no recommendation by default; Foundation §10.6)."),
        SkillSpec("executive.approval_presenter", SkillCategory.EXECUTIVE, 5_000, _L1, 0,
                  "Format ApprovalRequest for user; risk level -> confirmation requirement."),
        SkillSpec("executive.synthesis_diff_presenter", SkillCategory.EXECUTIVE, 10_000, _L1, 1,
                  "Format synthesis diff report; separate auto-displayable inferred from speculative."),
        # -- Category 5: Evaluative (5) -----------------------------------
        SkillSpec("evaluative.output_validator", SkillCategory.EVALUATIVE, 5_000, _L0, 0,
                  "Validate agent output against output_format contract; called on every skill return."),
        SkillSpec("evaluative.confidence_auditor", SkillCategory.EVALUATIVE, 15_000, _L0, 1,
                  "Review inferred/speculative nodes from current session; verify evidentiary chain."),
        SkillSpec("evaluative.skill_performance_tracker", SkillCategory.EVALUATIVE, 10_000, _L0, 1,
                  "Update rolling 30-day performance metrics for skills invoked this session."),
        SkillSpec("evaluative.loop_health_checker", SkillCategory.EVALUATIVE, 30_000, _L0, 2,
                  "Evaluate health of all loop types over past 7 days; used by meta-loop."),
        SkillSpec("evaluative.memory_integrity_checker", SkillCategory.EVALUATIVE, 20_000, _L0, 1,
                  "Scan nodes for schema violations: missing pillar, empty synthesis_from, etc."),
        # -- Category 6: Safety (5) — bypass invoke_skill wrapper ---------
        SkillSpec("safety.authority_checker", SkillCategory.SAFETY, 2_000, _SYS, 0,
                  "Validate agent authority before any action; synchronous; logs INV-01 violations."),
        SkillSpec("safety.loop_watchdog", SkillCategory.SAFETY, 1_000, _SYS, 0,
                  "Async background task; polls loop_runs; forces termination at max limits (INV-08)."),
        SkillSpec("safety.contradiction_enforcer", SkillCategory.SAFETY, 5_000, _SYS, 0,
                  "On contradicts link: flag both nodes + create link + insert escalation (INV-07)."),
        SkillSpec("safety.approval_enforcer", SkillCategory.SAFETY, 3_000, _SYS, 0,
                  "Inside action_gateway: verify action_log has user_confirmed=true (INV-05)."),
        SkillSpec("safety.rollback_executor", SkillCategory.SAFETY, 5_000, _SYS, 0,
                  "Archive errant node (status='archived'); never deletes (INV-02); insert escalation."),
    ]
}

assert len(SKILL_CATALOG) == 34, f"catalog must list all 34 skills, found {len(SKILL_CATALOG)}"


# Safety skills that are implemented and invoked *directly* (they bypass
# invoke_skill / SKILL_REGISTRY, per the Safety Skill Rule), so they must
# be counted Active even though they never appear in SKILL_REGISTRY.
# rollback_executor is intentionally absent until its real Phase-1
# implementation lands (EC-27); until then it stays Draft.
_DIRECTLY_INVOKED_ACTIVE: set[str] = {
    "safety.authority_checker",      # also in SKILL_REGISTRY; listed for clarity
    "safety.loop_watchdog",
    "safety.contradiction_enforcer",
    "safety.approval_enforcer",
    "safety.rollback_executor",      # real Phase-1 implementation landed (EC-27)
}


def active_skill_names() -> set[str]:
    """Names of skills that are genuinely invocable right now.

    Derived from the runtime registry plus the directly-invoked safety
    set, so there is no separate hand-maintained "which skills are done"
    list to drift out of sync.
    """
    from aether.skills.registry import SKILL_REGISTRY  # lazy: avoids import cycle

    return set(SKILL_REGISTRY) | _DIRECTLY_INVOKED_ACTIVE


async def seed_skill_catalog(db: AsyncSession, *, commit: bool = True) -> dict[str, int]:
    """Idempotently upsert all 34 catalog skills into the ``skills`` table.

    Active status is derived (see ``active_skill_names``). Upsert key is
    (name, version) — the table's ``uq_skills_name_version`` constraint.
    Re-running after a new skill is registered flips it Draft->Active.

    Returns a small summary dict ``{"active": n, "draft": m}`` for logging.
    Runs as whatever role the session authenticates as; ``aether_app_role``
    holds SELECT/INSERT/UPDATE on ``skills`` (migration 0004), which is all
    this needs — no DELETE.
    """
    active = active_skill_names()
    counts = {"active": 0, "draft": 0}

    for spec in SKILL_CATALOG.values():
        status = "active" if spec.name in active else "draft"
        counts["active" if status == "active" else "draft"] += 1
        await db.execute(
            text(
                """
                INSERT INTO skills (name, category, version, status, timeout_ms, description)
                VALUES (:name, CAST(:category AS skill_category), :version,
                        CAST(:status AS skill_status), :timeout_ms, :description)
                ON CONFLICT (name, version) DO UPDATE SET
                    category    = EXCLUDED.category,
                    status      = EXCLUDED.status,
                    timeout_ms  = EXCLUDED.timeout_ms,
                    description = EXCLUDED.description
                """
            ),
            {
                "name": spec.name,
                "category": spec.category.value,
                "version": spec.version,
                "status": status,
                "timeout_ms": spec.timeout_ms,
                "description": spec.purpose,
            },
        )

    if commit:
        await db.commit()
    return counts
