"""
aether.skills.operational.node_writer
========================================

SKILL-14 (Phase-0 Prompt Section 15). Wraps ``write_node()`` as an
invokable skill — and, since the EC-15 layering fix, is now also where
the full detect-conflicts -> write -> enforce-contradictions sequence
Section 10 originally described lives, since that sequence requires
calling other skills (``cognitive.contradiction_detector``,
``safety.contradiction_enforcer``), which the memory layer itself is
not allowed to do (see ``aether/memory/write_protocol.py``'s
``write_node()`` docstring for the full explanation).
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aether.invariants.guards import (
    ConfidenceViolationError,
    NodeValidationError,
    SynthesisFromError,
)
from aether.memory.write_protocol import write_node
from aether.schemas.nodes import ConflictPair, ConflictResult, NodeDraft, WriteResult

logger = logging.getLogger("aether.skills.operational.node_writer")


async def write_node_with_contradiction_handling(
    draft: NodeDraft,
    session_id: UUID,
    requesting_agent: str,
    db: AsyncSession,
) -> WriteResult:
    """
    The full sequence Section 10 originally described: detect
    conflicts (step 4) -> write the node via the pure memory-layer
    primitive (steps 1,2,3,5,6,7a,7b,8,9,10) -> enforce contradictions
    post-commit (INV-07). Lives here, not in ``write_node()`` itself,
    so that ``aether.memory`` never imports from ``aether.skills``
    (CLAUDE.md's layering rule; see the EC-15 fix).
    """
    conflicts = await _run_contradiction_scan(draft, session_id, requesting_agent, db)

    result = await write_node(draft, session_id, requesting_agent, db)

    # Post-commit contradiction enforcement (Section 10, "After
    # transaction commits"). Deliberately outside write_node()'s own
    # transaction: contradiction_enforcer's job (INV-07) is to flag
    # both nodes and create an escalation, which must survive even if
    # something about this notification step itself were to fail
    # after the node write already succeeded.
    if conflicts:
        from aether.skills.safety.contradiction_enforcer import contradiction_enforcer

        for conflict in conflicts:
            await contradiction_enforcer(result.node_id, conflict.node_id, session_id, db)

    return WriteResult(
        node_id=result.node_id,
        status="written_with_contradiction" if conflicts else "written",
        contradiction_node_ids=[c.node_id for c in conflicts],
    )


async def _run_contradiction_scan(
    draft: NodeDraft, session_id: UUID, requesting_agent: str, db: AsyncSession
) -> list[ConflictPair]:
    """
    Invokes ``cognitive.contradiction_detector`` through the skill
    invoker. Never aborts the write on a detected conflict — conflicts
    are recorded and handled post-commit (INV-07). This is a
    skills-to-skills call (this module and the invoker are both in the
    skills/ layer), which is layering-legal, unlike the memory-layer
    call this replaced.
    """
    from aether.skills.invoker import invoke_skill

    result = await invoke_skill(
        "cognitive.contradiction_detector",
        {
            "candidate": {
                "title": draft.title,
                "content": draft.content,
                "pillar": draft.primary_pillar.value,
            },
            "pillar": draft.primary_pillar.value,
        },
        session_id,
        requesting_agent,
        None,
        db,
    )
    if result.status != "ok" or not result.output:
        logger.warning(
            "Contradiction scan did not complete successfully; proceeding without "
            "conflict data (fail-open by design — a scan failure must never block "
            "a write)",
            extra={"status": result.status},
        )
        return []
    conflict_result = ConflictResult.model_validate(result.output)
    return conflict_result.conflicts


async def write_node_skill(inputs: dict, db) -> dict:
    """
    inputs: ``{'draft': {NodeDraft fields}, 'requesting_agent': str, 'session_id': str}``.
    Returns a ``WriteResult`` dict on success, or
    ``{'error': True, 'code': str, 'message': str}`` on a known failure
    mode (FM-01 through FM-03). FM-04 (DB failure) is allowed to
    propagate — ``write_node()`` already rolls back and logs it; a
    generic DB failure isn't one of the three named codes, so it isn't
    swallowed here.
    """
    draft = NodeDraft.model_validate(inputs["draft"])
    session_id = inputs["session_id"]
    requesting_agent = inputs["requesting_agent"]

    try:
        result = await write_node_with_contradiction_handling(draft, session_id, requesting_agent, db)
    except ConfidenceViolationError as exc:
        return {"error": True, "code": "inv03_violation", "message": str(exc)}
    except SynthesisFromError as exc:
        return {"error": True, "code": "synthesis_from_required", "message": str(exc)}
    except NodeValidationError as exc:
        return {"error": True, "code": "node_validation_error", "message": str(exc)}

    return result.model_dump(mode="json")
