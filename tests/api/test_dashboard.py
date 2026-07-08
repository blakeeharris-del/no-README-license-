"""
tests.api.test_dashboard — Level 2 Dashboard (AETHER_DASHBOARD_SPEC_v1.0).

The forced criterion is EC-34 / §8 Data accuracy: every value shown matches
the Memory Layer's live state at load time, with no cached or placeholder
values. Proven by seeding known live state, loading the dashboard, asserting
the displayed values equal the seed, then MUTATING the live state and
confirming a reload reflects the change (proving the binding is live, not
cached).

EC-33 / §8: the six zones are present and Section 7's exclusions are absent
— asserted structurally where the medium allows (exactly one command input,
no avatar, no animation, no desktop scroll, flat surfaces, four status
states). Purely visual confirmations are flagged for Blake's review, not
claimed as passing here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import update

from aether.api.main import app
from aether.api.routes.dashboard import build_dashboard, render_dashboard_html
from aether.models.enums import (
    ConfidenceLevel,
    CreatedByAgent,
    EscalationStatus,
    EscalationType,
    NodeSource,
    NodeStatus,
    PillarName,
    PriorityClass,
)
from aether.models.nodes import Node, NodePillar
from aether.models.runtime import PendingEscalation
from aether.schemas.dashboard import Dashboard, DashboardStatus


async def _node(db, session_id, pillar, title, *, deadline_days=None):
    md = {}
    if deadline_days is not None:
        md["deadline"] = (datetime.now(timezone.utc) + timedelta(days=deadline_days)).isoformat()
    node = Node(
        type="fact", title=title, content="c", source=NodeSource.USER_EXPLICIT,
        confidence=ConfidenceLevel.EXPLICIT, status=NodeStatus.ACTIVE,
        created_by=CreatedByAgent.USER, session_id=session_id, metadata_=md,
    )
    db.add(node)
    await db.flush()
    db.add(NodePillar(node_id=node.id, pillar=pillar, is_primary=True, assigned_by=CreatedByAgent.USER))
    await db.commit()
    return node


async def _approval(db, session_id, title, *, action_log_id=None, pillar="legal"):
    content = {"title": title, "description": f"{title} — effect", "pillar": pillar}
    if action_log_id:
        content["action_log_id"] = action_log_id
    esc = PendingEscalation(
        escalation_type=EscalationType.CLARIFICATION, priority_class=PriorityClass.P1,
        content=content, session_id=session_id, status=EscalationStatus.PENDING,
    )
    db.add(esc)
    await db.commit()
    return esc


# ---------------------------------------------------------------------------
# EC-34: forced live binding (seed -> load -> assert -> mutate -> reload)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ec34_displayed_values_equal_live_state_and_reflect_mutation(db_session, test_session_row):
    sid = test_session_row.id
    legal = await _node(db_session, sid, PillarName.LEGAL, "File response brief", deadline_days=3)
    await _node(db_session, sid, PillarName.HEALTH, "Annual checkup notes")
    await _approval(db_session, sid, "Send offer email")

    dash1 = await build_dashboard(db_session)

    # Displayed values equal the seeded live state (not placeholders):
    legal_tile = next(t for t in dash1.pillars.tiles if t.pillar == PillarName.LEGAL)
    assert legal_tile.status == DashboardStatus.ATTENTION          # deadline in 3 days
    assert legal_tile.top_item == "File response brief"            # the seeded title, verbatim
    health_tile = next(t for t in dash1.pillars.tiles if t.pillar == PillarName.HEALTH)
    assert health_tile.status == DashboardStatus.ON_TRACK
    assert dash1.approvals.cards[0].action == "Send offer email"
    assert dash1.header.pending_reviews == 1                       # queue count = live approvals
    assert any(i.what == "File response brief" and i.status == DashboardStatus.ATTENTION
               for i in dash1.today.items)                         # the live deadline
    assert "File response brief" in [m.description for m in dash1.memory.rows]

    # Mutate the live state: resolve the approval, add a Business node.
    await db_session.execute(
        update(PendingEscalation).where(PendingEscalation.session_id == sid)
        .values(status=EscalationStatus.RESOLVED)
    )
    await db_session.commit()
    await _node(db_session, sid, PillarName.BUSINESS, "Q3 revenue review")

    # Reload — the same call, fresh reads: reflects the mutation (no cache).
    dash2 = await build_dashboard(db_session)
    assert dash2.header.pending_reviews == 0                       # approval gone
    assert dash2.approvals.cards == []
    biz_tile = next(t for t in dash2.pillars.tiles if t.pillar == PillarName.BUSINESS)
    assert biz_tile.top_item == "Q3 revenue review"               # new live node visible


# ---------------------------------------------------------------------------
# EC-33: six zones present (Section 2) + status vocabulary (Section 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ec33_six_zones_present_fixed_pillar_order(db_session):
    dash = await build_dashboard(db_session)
    # Exactly the six zones of Section 2 — none added, none omitted.
    assert Dashboard.ZONES == ("input_bar", "today", "pillars", "approvals", "memory", "files")
    for zone in Dashboard.ZONES:
        assert getattr(dash, zone) is not None
    # Pillars: six tiles, fixed order (6.4).
    assert [t.pillar for t in dash.pillars.tiles] == [
        PillarName.LEGAL, PillarName.PERSONAL_FINANCE, PillarName.CAREER,
        PillarName.BUSINESS, PillarName.HEALTH, PillarName.RELATIONSHIPS,
    ]
    # Empty pillar is a calm valid state, not a gap.
    assert all(t.top_item for t in dash.pillars.tiles)


def test_status_vocabulary_is_exactly_four_states():
    assert {s.value for s in DashboardStatus} == {"attention", "on_track", "new", "scheduled"}


# ---------------------------------------------------------------------------
# EC-33: Section 7 exclusions absent (asserted on the rendered DOM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ec33_exclusions_absent_in_rendered_view(db_session, test_session_row):
    await _node(db_session, test_session_row.id, PillarName.LEGAL, "Due soon", deadline_days=2)
    html = render_dashboard_html(await build_dashboard(db_session))

    # No orb/pulse/ambient animation of any kind (Section 7).
    assert "@keyframes" not in html
    assert "animation" not in html
    # No persona avatar/face/photograph (Rule Persona / Section 7).
    assert "<img" not in html and "avatar" not in html.lower()
    # One command input surface; the only other input is the Memory search
    # (6.6, explicitly the sole search surface). No FAB / "+" / palette.
    assert html.count("<input") == 2
    assert html.count('id="input-bar"') == 1        # the single command input
    assert html.count('id="memory-search"') == 1    # the single search input
    for banned in ('class="fab"', "command-palette", "floating-action"):
        assert banned not in html
    # No scrolling on the desktop layout (Section 7).
    assert "overflow:hidden" in html
    assert "overflow:auto" not in html and "overflow:scroll" not in html
    # Flat surfaces — no drop shadows, gradients, or glow (Rule Surfaces).
    for banned in ("box-shadow", "gradient", "blur("):
        assert banned not in html
    # One accent color, defined once (Rule Theme).
    assert html.count("--accent:") == 1
    # All six zones render as data-zone regions.
    for zone in Dashboard.ZONES:
        assert f'data-zone="{zone}"' in html


@pytest.mark.asyncio
async def test_trust_evidence_is_read_only_in_memory_zone(db_session):
    """The trust-maturity evidence surfaces in the Memory zone (no new zone)
    and is READ-ONLY — loading the dashboard advances nothing."""
    from aether.memory.trust_state import current_trust_stage

    before = await current_trust_stage(db_session)
    dash = await build_dashboard(db_session)

    te = dash.memory.trust_evidence
    assert te is not None
    assert te.current_stage == before                 # reflects the live stage
    assert te.signals                                  # real evidence signals present
    # Loading the dashboard did not advance the stage.
    assert await current_trust_stage(db_session) == before
    # It renders inside the existing Memory zone — no seventh zone added.
    assert Dashboard.ZONES == ("input_bar", "today", "pillars", "approvals", "memory", "files")


@pytest.mark.asyncio
async def test_user_content_is_html_escaped(db_session, test_session_row):
    """Bug fixed: node titles are user content and must be escaped when
    rendered — otherwise a title like '<input>' or '<script>' injects
    markup (stored XSS) and breaks the structural exclusion counts."""
    await _node(db_session, test_session_row.id, PillarName.LEGAL,
                "<script>alert(1)</script>")
    html = render_dashboard_html(await build_dashboard(db_session))
    assert "<script>alert(1)</script>" not in html   # not rendered raw
    assert "&lt;script&gt;" in html                   # escaped instead
    assert html.count("<input") == 2                  # injection can't add inputs


# ---------------------------------------------------------------------------
# §8 Approval integrity: cards carry the id that routes to the Gateway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_card_carries_action_log_id_for_gateway_routing(db_session, test_session_row):
    await _approval(db_session, test_session_row.id, "Wire transfer",
                    action_log_id="abc-123")
    dash = await build_dashboard(db_session)
    card = next(c for c in dash.approvals.cards if c.action == "Wire transfer")
    # Approve/Modify/Reject route to /approve via this id (INV-05); no card
    # is resolvable without one of those explicit actions.
    assert card.action_log_id == "abc-123"


# ---------------------------------------------------------------------------
# Endpoint smoke: both surfaces serve the live model
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.mark.asyncio
async def test_dashboard_endpoints_serve_live_model(client):
    r = await client.get("/dashboard")
    assert r.status_code == 200
    body = r.json()
    for zone in Dashboard.ZONES:
        assert zone in body

    v = await client.get("/dashboard/view")
    assert v.status_code == 200
    assert "text/html" in v.headers["content-type"]
    assert 'data-zone="pillars"' in v.text
