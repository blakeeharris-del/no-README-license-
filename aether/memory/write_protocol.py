"""
aether.memory.write_protocol
==============================

``write_node()`` — the atomic 10-step write protocol (Phase-0 Prompt
Section 10). All 10 steps commit together, or none do.

Forward-reference note: steps 4 and the post-commit contradiction
pass call ``invoke_skill()`` (``aether/skills/invoker.py``, Step 12)
and, indirectly through it, ``cognitive.contradiction_detector``
(Step 15) and ``contradiction_enforcer`` (Step 14). None of those
exist yet at Step 8. This module is written now, per Section 3's
literal sequence, exactly as ``database.py`` referenced
``aether.config`` before Step 7 existed. It will not fully execute
end-to-end until Steps 12-14 are built; at that point steps 1, 2, 3,
5, 6, 7a, 7b, 8, 9, 10 have already been verified against real
Postgres in isolation (with the forward-referenced calls mocked) — see
the checkpoint notes for how that was done without building those
files early.

Ordering constraint, added once Step 12 existed: ``invoke_skill()``
commits the ``db`` session at both its START and COMPLETE logging
steps (INV-09), independent of whatever transaction its caller is in
the middle of. That is only safe to nest inside ``write_node()``'s own
transaction because step 4 (the contradiction scan, which calls
``invoke_skill()``) runs *before* any of write_node's own inserts
(step 6 onward) — at the moment step 4's commit fires, nothing from
this write is pending yet, so nothing of write_node's own is
prematurely committed. Do not move the contradiction scan to after
step 6 without re-examining this.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aether.invariants.guards import (
    NodeValidationError,
    SynthesisFromError,
    assert_confidence_not_explicit_from_agent,
)
from aether.models.enums import (
    ActionType,
    AgentName,
    ConfidenceLevel,
    LinkType,
    NodeSource,
    NodeStatus,
    PillarName,
)
from aether.models.logs import ActionLog
from aether.models.nodes import Node, NodeLink, NodePillar
from aether.schemas.nodes import NodeDraft, WriteResult

logger = logging.getLogger("aether.memory.write_protocol")

# Step 3's "most consequential pillar" ordering, most to least
# consequential, exactly as given in Section 10 step 3.
_PILLAR_CONSEQUENCE_ORDER = [
    "legal",
    "personal_finance",
    "business",
    "career",
    "health",
    "relationships",
]


async def write_node(
    draft: NodeDraft,
    session_id: UUID,
    requesting_agent: str,
    db: AsyncSession,
) -> WriteResult:
    """
    Write a single memory node through the atomic write protocol.

    Layering note (fixed after EC-15's import-linter check found a
    real violation): this function no longer performs contradiction
    detection or enforcement itself. Section 10's own pseudocode
    embeds a contradiction scan (step 4) and a post-commit
    ``contradiction_enforcer()`` call directly inside ``write_node()``
    — but doing so requires importing from ``aether.skills``, which
    directly violates CLAUDE.md's stated layering rule ("memory/
    imports from models/ only"). Those two requirements, both from the
    same document, are in direct conflict. Since EC-15 explicitly
    tests for zero cross-layer import violations, the layering rule is
    treated as authoritative here: contradiction detection and
    enforcement now live in the skill orchestration layer
    (``aether/skills/operational/node_writer.py``), which calls this
    function as a pure lower-layer primitive and handles the
    detect-then-write-then-enforce sequence itself — skills/ calling
    memory/ and skills/ calling skills/ are both layering-legal.
    ``write_node()`` itself is now purely steps 1, 2, 3, 5, 6, 7a, 7b,
    8, 9, 10 of the original 10-step protocol; it always returns
    ``status='written'`` with empty ``contradiction_node_ids`` — a
    caller that wants contradiction handling must go through
    ``aether.skills.operational.node_writer`` instead.

    On any exception, the entire transaction is rolled back, and a
    *separate*, out-of-transaction ``action_log`` row records the
    failure (Section 10's preamble instruction) — this second log
    write happens on its own connection/transaction specifically so it
    survives the rollback of the failed attempt.
    """
    try:
        result = await _write_node_transactional(draft, session_id, requesting_agent, db)
    except Exception as exc:
        await db.rollback()
        await _log_write_failure(session_id, requesting_agent, draft, exc, db)
        raise

    return WriteResult(node_id=result["node_id"], status="written", contradiction_node_ids=[])


async def _write_node_transactional(
    draft: NodeDraft,
    session_id: UUID,
    requesting_agent: str,
    db: AsyncSession,
) -> dict:
    """Steps 1,2,3,5,6,7a,7b,8,9,10 (step 4 moved to the skill layer). Returns ``{"node_id": UUID}``."""

    # ---- STEP 1: validate required fields ---------------------------
    _validate_draft(draft)

    # ---- STEP 2: enforce INV-03 --------------------------------------
    assert_confidence_not_explicit_from_agent(draft.created_by, draft.confidence, "write_node")
    if draft.created_by.value != "user" and draft.source == NodeSource.USER_EXPLICIT:
        raise NodeValidationError("Agent cannot claim source='user_explicit'")

    # ---- STEP 3: assign pillar(s) ------------------------------------
    pillars = list(draft.pillars)
    primary_pillar = draft.primary_pillar
    extra_metadata: dict = {}
    forced_status: NodeStatus | None = None
    if not pillars:
        # Belt-and-suspenders: step 1 already rejects an empty list,
        # but if somehow reached, assign the most consequential pillar.
        primary_pillar = PillarName(_PILLAR_CONSEQUENCE_ORDER[0])
        pillars = [primary_pillar]
        extra_metadata["auto_pillar_assigned"] = True
        forced_status = NodeStatus.FLAGGED

    # ---- STEP 5: supersession check -----------------------------------
    superseded_node_id = await _check_supersession(draft, primary_pillar, db)

    # ---- STEP 6: insert the node --------------------------------------
    metadata = dict(draft.metadata)
    metadata.update(extra_metadata)
    node = Node(
        type=draft.type,
        title=draft.title,
        content=draft.content,
        source=draft.source,
        confidence=draft.confidence,
        status=forced_status or NodeStatus.ACTIVE,
        expiry_policy=draft.expiry_policy,
        expiry_date=draft.expiry_date,
        created_by=draft.created_by,
        session_id=session_id,
        metadata_=metadata,
    )
    db.add(node)
    await db.flush()  # populate node.id without committing

    # ---- STEP 7a: node_pillars rows -----------------------------------
    for pillar in pillars:
        db.add(
            NodePillar(
                node_id=node.id,
                pillar=pillar,
                is_primary=(pillar == primary_pillar),
                assigned_by=draft.created_by,
            )
        )

    # ---- STEP 7b: supersession link ------------------------------------
    if superseded_node_id is not None:
        db.add(
            NodeLink(
                source_id=node.id,
                target_id=superseded_node_id,
                link_type=LinkType.SUPERSEDES,
                created_by=draft.created_by,
            )
        )

    # ---- STEP 8: synthesis_from validation -----------------------------
    if draft.source == NodeSource.SYNTHESIS:
        synthesis_from = metadata.get("synthesis_from")
        if not synthesis_from or not isinstance(synthesis_from, list):
            raise SynthesisFromError(
                "source='synthesis' node missing non-empty metadata.synthesis_from"
            )
        for ref in synthesis_from:
            try:
                UUID(str(ref))
            except (ValueError, AttributeError) as exc:
                raise SynthesisFromError(
                    f"metadata.synthesis_from contains a non-UUID entry: {ref!r}"
                ) from exc

    # ---- STEP 9: action_log row (INV-01, same transaction) -------------
    db.add(
        ActionLog(
            session_id=session_id,
            agent=AgentName(requesting_agent),
            action_type=ActionType.WRITE,
            node_ids=[node.id],
            input_summary=f"write {draft.type.value}: {draft.title[:100]}",
            user_confirmed=False,
        )
    )

    # ---- STEP 10: update L1 snapshot (same transaction) -----------------
    from aether.memory.session_state import rebuild_l1, save_l1_snapshot

    l1 = await rebuild_l1(session_id, db)
    await save_l1_snapshot(session_id, l1, db)

    await db.commit()

    return {"node_id": node.id}


def _validate_draft(draft: NodeDraft) -> None:
    """
    STEP 1. Pydantic's own validation already enforces most of this at
    ``NodeDraft`` construction time (title max_length=120, pillars
    min_length=1). This function re-asserts the same rules defensively
    at the write boundary, so that ``write_node()`` never silently
    trusts an already-validated-elsewhere object — a caller could in
    principle construct a ``NodeDraft`` via ``model_construct()``,
    bypassing validation.
    """
    if not draft.title or len(draft.title) > 120:
        raise NodeValidationError("title is required and must be <= 120 chars")
    if not draft.content:
        raise NodeValidationError("content is required")
    if not draft.pillars:
        raise NodeValidationError("pillars must be a non-empty list")
    if draft.primary_pillar not in draft.pillars:
        raise NodeValidationError("primary_pillar must be one of draft.pillars")


# _run_contradiction_scan() used to live here (step 4 of the original
# 10-step protocol), invoking aether.skills.invoker.invoke_skill() —
# which made this module import from aether.skills, violating
# CLAUDE.md's layering rule ("memory/ imports from models/ only"), a
# real violation EC-15's import-linter check caught. Moved to
# aether.skills.operational.node_writer, which orchestrates
# detect -> write -> enforce around this module's pure write_node()
# primitive. See write_node()'s docstring for the full explanation.


async def _check_supersession(draft: NodeDraft, primary_pillar, db: AsyncSession) -> UUID | None:
    """
    STEP 5. Looks for an existing active node in the same primary
    pillar whose title is a close-enough match to the draft's to be
    considered the same fact restated. Only treated as a real
    supersession if the draft is itself ``confidence='explicit'``
    (i.e. the user is correcting the record).

    Deviation from a literal reading of Section 10 step 5, flagged for
    review: the spec's literal text is "fulltext title match score >
    0.85", which this implementation initially took to mean
    ``ts_rank(...) > 0.85``. Testing against real Postgres showed that
    ``ts_rank`` for two *identical* titles is only ~0.64 — the 0.85
    threshold could never fire, even on an exact match. This is
    exactly the ambiguity AETHER_MISSING_SPECS_v1.0's GAP-03
    ("Supersession Matching Method") is written to resolve — it is
    explicitly labeled a "[PHASE-0 BLOCKER]" there, with two named
    options: (A) an LLM same-fact comparison call, or (B) simple
    token-overlap / Levenshtein similarity with the same > 0.85
    threshold, "simpler but less accurate," explicitly valid for
    Phase-0 (Phase-1 replaces it with pgvector cosine similarity).
    Option A requires a synchronous LLM call from inside
    ``write_node()``'s open DB transaction, which is architecturally
    awkward (network I/O holding a transaction open, complicated
    rollback semantics) and not how any other step in this protocol
    behaves. Option B is implemented here: Postgres fulltext search
    narrows pillar-scoped candidates efficiently, then Jaccard
    token-overlap in Python applies the actual > 0.85 threshold.
    """
    tsvector = func.to_tsvector("english", Node.title)
    tsquery = func.plainto_tsquery("english", draft.title)
    stmt = (
        select(Node.id, Node.title)
        .join(NodePillar, NodePillar.node_id == Node.id)
        .where(
            NodePillar.pillar == primary_pillar,
            Node.status == NodeStatus.ACTIVE,
            Node.source.in_([NodeSource.USER_EXPLICIT, NodeSource.AGENT_WRITE]),
            tsvector.op("@@")(tsquery),
        )
        .limit(20)
    )
    candidates = (await db.execute(stmt)).all()
    if not candidates:
        return None

    draft_tokens = _tokenize(draft.title)
    best_match_id: UUID | None = None
    best_score = 0.0
    for node_id, title in candidates:
        score = _jaccard_token_overlap(draft_tokens, _tokenize(title))
        if score > best_score:
            best_score, best_match_id = score, node_id

    if best_match_id is not None and best_score > 0.85 and draft.confidence == ConfidenceLevel.EXPLICIT:
        # Mark the old node superseded now; the new node doesn't exist
        # yet at this point in the protocol (step 6 hasn't run), so the
        # NodeLink row referencing it is created in step 7b instead.
        await db.execute(
            update(Node).where(Node.id == best_match_id).values(status=NodeStatus.SUPERSEDED)
        )
        return best_match_id
    return None


def _tokenize(text_value: str) -> set[str]:
    """Lowercase whitespace tokenization for Jaccard overlap scoring."""
    return set(text_value.lower().split())


def _jaccard_token_overlap(a: set[str], b: set[str]) -> float:
    """|intersection| / |union|, per GAP-03 Option B's token_overlap()."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


async def _log_write_failure(
    session_id: UUID,
    requesting_agent: str,
    draft: NodeDraft,
    exc: Exception,
    db: AsyncSession,
) -> None:
    """
    Records a failed write attempt in a fresh transaction, separate
    from the one that just rolled back — per Section 10's preamble:
    "log error to action_log OUTSIDE the transaction (separate call)."
    """
    try:
        db.add(
            ActionLog(
                session_id=session_id,
                agent=AgentName(requesting_agent),
                action_type=ActionType.WRITE,
                node_ids=[],
                input_summary=f"write {draft.type.value}: {draft.title[:100]}",
                output_summary=f"failed: {type(exc).__name__}"[:500],
                user_confirmed=False,
            )
        )
        await db.commit()
    except Exception:
        # Logging the failure must never raise a second exception that
        # masks the original one.
        logger.exception("Failed to record write-failure action_log entry")
        await db.rollback()
