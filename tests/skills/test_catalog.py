"""
tests.skills.test_catalog
===========================

Verifies the EC-19 backbone added in Phase-1:
  - the catalog lists exactly 34 skills;
  - seeding is idempotent and yields 12 Active / 22 Draft against the
    real DB (matching Step-0's inventory: 12 working, 22 to build);
  - invoke_skill's Active-gate (Foundation §10.7.1) refuses to run a
    skill whose skills-table status is Draft, and logs the refusal.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from aether.invariants.guards import SkillNotActiveError
from aether.models.runtime import SkillInvocationLog
from aether.skills.catalog import SKILL_CATALOG, active_skill_names, seed_skill_catalog


def test_catalog_has_all_34_skills():
    assert len(SKILL_CATALOG) == 34


def test_catalog_active_split_matches_inventory():
    """Active ⟺ genuinely invocable (registry + directly-invoked safety).

    With the full Phase-1 skill build complete, all 34 are implemented
    and Active; this asserts the derivation holds and totals 34.
    """
    active = active_skill_names()
    catalog_names = set(SKILL_CATALOG)
    active_in_catalog = active & catalog_names
    draft = catalog_names - active
    assert len(active_in_catalog) + len(draft) == 34
    # EC-27: rollback_executor's real implementation has landed -> Active.
    assert "safety.rollback_executor" in active
    assert len(draft) == 0  # all 34 skills Active


@pytest.mark.asyncio
async def test_seed_is_idempotent(db_session):
    active = active_skill_names() & set(SKILL_CATALOG)
    expected = {"active": len(active), "draft": 34 - len(active)}
    first = await seed_skill_catalog(db_session, commit=False)
    second = await seed_skill_catalog(db_session, commit=False)
    assert first == second == expected
    total = (
        await db_session.execute(text("SELECT count(*) FROM skills"))
    ).scalar_one()
    assert total == 34  # no duplicate rows created on re-seed


@pytest.mark.asyncio
async def test_active_gate_refuses_draft_skill(db_session, test_session_row, monkeypatch):
    """A registered skill that is Draft in the table must not run."""
    async def draftonly(inputs, db):  # pragma: no cover - must never execute
        return {"ran": True}

    import aether.skills.registry as reg

    monkeypatch.setitem(reg.SKILL_REGISTRY, "test.draftonly", draftonly)
    monkeypatch.setitem(reg.SKILL_TIMEOUTS, "test.draftonly", 5000)
    # Seed it explicitly as DRAFT (not active).
    await db_session.execute(
        text(
            "INSERT INTO skills (name, category, version, status, timeout_ms) "
            "VALUES ('test.draftonly', 'operational', '1.0.0', 'draft', 5000) "
            "ON CONFLICT (name, version) DO UPDATE SET status='draft'"
        )
    )
    await db_session.flush()

    from aether.skills.invoker import invoke_skill

    with pytest.raises(SkillNotActiveError):
        await invoke_skill("test.draftonly", {}, test_session_row.id, "master", None, db_session)

    # The refusal must still be logged (INV-09), marked error/skill_not_active.
    log = (
        await db_session.execute(
            select(SkillInvocationLog).where(SkillInvocationLog.skill_name == "test.draftonly")
        )
    ).scalar_one()
    assert log.status == "error"
    assert log.error_detail == "skill_not_active"
