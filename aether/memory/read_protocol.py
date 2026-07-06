"""
aether.memory.read_protocol
=============================

The 7 read-side query functions (Phase-0 Prompt Section 11), all
applying the INV-04 filter by default.

INV-04 STANDARD FILTER (Section 11):
    WHERE status IN ('active', 'pending_review')
    AND NOT (confidence = 'speculative' AND status = 'pending_review')

Every function that builds context for reasoning applies this filter
unless explicitly told not to (``bypass_inv04=True``), which is
reserved for contradiction scans and synthesis inspection — the two
legitimate cases that need visibility into every node regardless of
confidence tier.

Two under-specified details, resolved here and flagged for review:

1. ``NodeSummary.link_count`` — Section 6 defines the field but no
   section specifies how it's computed. Implemented as the total count
   of ``node_links`` rows where the node appears as either
   ``source_id`` or ``target_id`` (i.e. degree in the link graph,
   undirected count).
2. ``traverse_links()`` — Section 11 doesn't state a direction.
   Implemented as: nodes reachable as the *target* of a link
   ``FROM node_id`` of the given ``link_type`` (i.e. "what does this
   node point to via this link type"), which matches how
   ``write_protocol`` creates supersession links (new node is
   ``source_id``, old node is ``target_id``). The INV-04 filter is
   applied to the results, consistent with every other read function
   in this module that doesn't take a ``bypass_inv04`` parameter.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import TIMESTAMP, and_, func, not_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.enums import ConfidenceLevel, LinkType, NodeSource, NodeStatus, NodeType, PillarName
from aether.models.nodes import Node, NodeLink, NodePillar
from aether.schemas.nodes import DeadlineRef, NodeSummary


def _inv04_clause():
    """
    The INV-04 standard filter, as a SQLAlchemy boolean clause:
    status must be active or pending_review, and speculative+
    pending_review nodes are excluded even though pending_review
    passes the first half of the filter.
    """
    return and_(
        Node.status.in_([NodeStatus.ACTIVE, NodeStatus.PENDING_REVIEW]),
        not_(
            and_(
                Node.confidence == ConfidenceLevel.SPECULATIVE,
                Node.status == NodeStatus.PENDING_REVIEW,
            )
        ),
    )


async def _to_node_summaries(nodes: list[Node], db: AsyncSession) -> list[NodeSummary]:
    """
    Batch-loads pillars and link counts for a list of ORM ``Node``
    rows and converts them into ``NodeSummary`` schemas. Batched (not
    N+1) since context assembly can pull dozens of nodes at once.
    """
    if not nodes:
        return []
    node_ids = [n.id for n in nodes]

    pillar_rows = (
        await db.execute(select(NodePillar.node_id, NodePillar.pillar).where(NodePillar.node_id.in_(node_ids)))
    ).all()
    pillars_by_node: dict[UUID, list[PillarName]] = {}
    for node_id, pillar in pillar_rows:
        pillars_by_node.setdefault(node_id, []).append(pillar)

    link_rows = (
        await db.execute(
            select(NodeLink.source_id, NodeLink.target_id).where(
                or_(NodeLink.source_id.in_(node_ids), NodeLink.target_id.in_(node_ids))
            )
        )
    ).all()
    link_count_by_node: dict[UUID, int] = {}
    for source_id, target_id in link_rows:
        if source_id in node_ids:
            link_count_by_node[source_id] = link_count_by_node.get(source_id, 0) + 1
        if target_id in node_ids:
            link_count_by_node[target_id] = link_count_by_node.get(target_id, 0) + 1

    summaries = []
    for node in nodes:
        deadline = node.metadata_.get("deadline") if node.metadata_ else None
        summaries.append(
            NodeSummary(
                id=node.id,
                type=node.type,
                title=node.title,
                content=node.content,
                confidence=node.confidence,
                status=node.status,
                pillars=pillars_by_node.get(node.id, []),
                deadline=deadline,
                created_at=node.created_at,
                link_count=link_count_by_node.get(node.id, 0),
            )
        )
    return summaries


async def read_by_pillar(
    pillars: list[PillarName], db: AsyncSession, bypass_inv04: bool = False
) -> list[NodeSummary]:
    """All nodes assigned to any of ``pillars``, INV-04 filtered by default."""
    stmt = (
        select(Node)
        .join(NodePillar, NodePillar.node_id == Node.id)
        .where(NodePillar.pillar.in_(pillars))
        .distinct()
    )
    if not bypass_inv04:
        stmt = stmt.where(_inv04_clause())
    nodes = (await db.execute(stmt)).scalars().all()
    return await _to_node_summaries(list(nodes), db)


async def read_by_deadline(
    pillars: list[PillarName], from_dt: datetime, to_dt: datetime, db: AsyncSession
) -> list[DeadlineRef]:
    """
    Nodes with a ``metadata.deadline`` in ``[from_dt, to_dt]``, plus any
    overdue deadline (``deadline < now()``) regardless of the INV-04
    filter — a deadline that has already passed must surface even if
    it would otherwise be excluded (e.g. a speculative+pending_review
    node the user never confirmed). Sorted by deadline ascending.
    """
    deadline_expr = func.cast(Node.metadata_["deadline"].astext, TIMESTAMP(timezone=True))

    base_where = and_(
        Node.metadata_["deadline"].isnot(None),
        NodePillar.pillar.in_(pillars),
    )
    in_range = and_(base_where, deadline_expr >= from_dt, deadline_expr <= to_dt, _inv04_clause())
    overdue = and_(base_where, deadline_expr < func.now())

    stmt = (
        select(Node, deadline_expr.label("deadline_ts"))
        .join(NodePillar, NodePillar.node_id == Node.id)
        .where(or_(in_range, overdue))
        .order_by(text("deadline_ts ASC"))
        .distinct()
    )
    rows = (await db.execute(stmt)).all()

    refs: list[DeadlineRef] = []
    from datetime import timezone

    now = datetime.now(timezone.utc)
    seen: set[UUID] = set()
    for node, deadline_ts in rows:
        if node.id in seen:
            continue
        seen.add(node.id)
        pillar_rows = (
            await db.execute(select(NodePillar.pillar).where(NodePillar.node_id == node.id, NodePillar.is_primary == True))  # noqa: E712
        ).first()
        pillar = pillar_rows[0] if pillar_rows else pillars[0]
        days_until = (deadline_ts - now).days
        refs.append(
            DeadlineRef(
                node_id=node.id,
                title=node.title,
                deadline=deadline_ts.isoformat(),
                pillar=pillar,
                days_until=days_until,
            )
        )
    refs.sort(key=lambda r: r.deadline)
    return refs


async def read_by_type(
    node_type: NodeType, pillars: list[PillarName], db: AsyncSession
) -> list[NodeSummary]:
    """Nodes of a given type within the given pillars, INV-04 filtered."""
    stmt = (
        select(Node)
        .join(NodePillar, NodePillar.node_id == Node.id)
        .where(Node.type == node_type, NodePillar.pillar.in_(pillars), _inv04_clause())
        .distinct()
    )
    nodes = (await db.execute(stmt)).scalars().all()
    return await _to_node_summaries(list(nodes), db)


async def read_pillar_nodes(
    pillars: list[PillarName],
    db: AsyncSession,
    node_types: list[NodeType] | None = None,
) -> list[Node]:
    """Full ORM ``Node`` rows for ``pillars`` (INV-04 filtered), optionally
    restricted to ``node_types``.

    ``read_by_pillar`` projects to ``NodeSummary``, which deliberately
    omits ``metadata_``. The Phase-1 analytical skills compute over that
    metadata (asset/liability amounts, relationship tiers, deadline
    obligation types), so they need the ORM rows. This helper reuses the
    single INV-04 clause rather than re-deriving the filter in each skill.
    """
    stmt = (
        select(Node)
        .join(NodePillar, NodePillar.node_id == Node.id)
        .where(NodePillar.pillar.in_(pillars), _inv04_clause())
    )
    if node_types:
        stmt = stmt.where(Node.type.in_(node_types))
    nodes = (await db.execute(stmt.distinct())).scalars().all()
    return list(nodes)


async def fulltext_search(
    query: str,
    pillar: PillarName,
    db: AsyncSession,
    limit: int = 20,
    bypass_inv04: bool = False,
) -> list[NodeSummary]:
    """
    Postgres fulltext search over ``title || ' ' || content``, scoped
    to a single pillar, ordered by ``ts_rank`` descending.
    """
    tsvector = func.to_tsvector("english", Node.title + " " + Node.content)
    tsquery = func.plainto_tsquery("english", query)
    rank = func.ts_rank(tsvector, tsquery).label("rank")

    stmt = (
        select(Node)
        .join(NodePillar, NodePillar.node_id == Node.id)
        .where(NodePillar.pillar == pillar, tsvector.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    )
    if not bypass_inv04:
        stmt = stmt.where(_inv04_clause())
    nodes = (await db.execute(stmt)).scalars().all()
    return await _to_node_summaries(list(nodes), db)


async def traverse_links(node_id: UUID, link_type: LinkType, db: AsyncSession) -> list[NodeSummary]:
    """
    Nodes reachable as the *target* of a ``link_type`` link originating
    at ``node_id``. See module docstring for the direction convention
    chosen here. INV-04 filtered.
    """
    stmt = (
        select(Node)
        .join(NodeLink, NodeLink.target_id == Node.id)
        .where(NodeLink.source_id == node_id, NodeLink.link_type == link_type, _inv04_clause())
    )
    nodes = (await db.execute(stmt)).scalars().all()
    return await _to_node_summaries(list(nodes), db)


async def fetch_l3(pillar: PillarName, db: AsyncSession, limit: int = 5) -> list[NodeSummary]:
    """
    L3 = ``source='synthesis'`` and ``status IN ('active',
    'pending_review')``. INV-04 filtered, most recent first.
    """
    stmt = (
        select(Node)
        .join(NodePillar, NodePillar.node_id == Node.id)
        .where(
            NodePillar.pillar == pillar,
            Node.source == NodeSource.SYNTHESIS,
            Node.status.in_([NodeStatus.ACTIVE, NodeStatus.PENDING_REVIEW]),
            _inv04_clause(),
        )
        .order_by(Node.created_at.desc())
        .limit(limit)
        .distinct()
    )
    nodes = (await db.execute(stmt)).scalars().all()
    return await _to_node_summaries(list(nodes), db)


async def scan_contradictions(db: AsyncSession, pillar: PillarName | None = None) -> list[dict]:
    """
    All ``node_links`` rows with ``link_type='contradicts'``.
    ``bypass_inv04=True`` in spirit — this function deliberately does
    not filter by node status/confidence at all, since a full
    contradiction scan (INV-07) must see every contradiction
    regardless of the involved nodes' confidence tier.
    """
    stmt = select(NodeLink.source_id, NodeLink.target_id, NodeLink.id).where(
        NodeLink.link_type == LinkType.CONTRADICTS
    )
    if pillar is not None:
        source_pillars = select(NodePillar.node_id).where(NodePillar.pillar == pillar)
        stmt = stmt.where(
            or_(NodeLink.source_id.in_(source_pillars), NodeLink.target_id.in_(source_pillars))
        )
    rows = (await db.execute(stmt)).all()
    return [
        {"node_id_a": str(source_id), "node_id_b": str(target_id), "link_id": str(link_id)}
        for source_id, target_id, link_id in rows
    ]
