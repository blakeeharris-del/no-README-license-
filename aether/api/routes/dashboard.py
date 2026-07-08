"""
aether.api.routes.dashboard
=============================

The Level 2 Dashboard (AETHER_DASHBOARD_SPEC_v1.0). A presentation layer —
a live view over the Memory Layer, assembled fresh on every load (no
caching, no placeholders: EC-34 / §8 Data accuracy). Two surfaces:

  GET /dashboard        -> the live six-zone Dashboard model (JSON)
  GET /dashboard/view   -> a server-rendered HTML view of that model

Every value comes from a live read at request time. Nothing here adds a
new agent, skill, or data model (§1) — it only displays what the Memory
Layer already holds at Confidence Tier Explicit/Corroborated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape as _esc

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, func, select

from aether.database import get_db
from aether.memory.read_protocol import read_pillar_nodes
from aether.models.enums import (
    EscalationStatus,
    EscalationType,
    NodeStatus,
    SessionStatus,
)
from aether.models.nodes import Node, NodeLink
from aether.models.runtime import PendingEscalation, SkillInvocationLog
from aether.models.sessions import Session
from aether.schemas.dashboard import (
    PILLAR_ICON,
    PILLAR_LABEL,
    PILLAR_ORDER,
    ApprovalCard,
    Approvals,
    Dashboard,
    DashboardStatus,
    Files,
    HeaderIndicators,
    InputBar,
    Memory,
    MemoryRow,
    Pillars,
    PillarItem,
    PillarTile,
    Today,
    TodayItem,
    TrustEvidenceView,
    TrustSignal,
)
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_SUGGESTIONS = ["Brief me", "What changed", "Prepare meeting"]  # 6.2 first-load chips


def _parse_deadline(node: Node) -> datetime | None:
    raw = (node.metadata_ or {}).get("deadline")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def _node_status(node: Node, now: datetime) -> DashboardStatus:
    """Section 5 status, derived from live node state (deterministic, no LLM).

    Spec ambiguity surfaced and ruled here (flagged for Blake / HANDOFF):
    Section 5's ``New`` state is defined by its revert-on-view behavior —
    "Surfaced since the last session close; not yet seen. Reverts to On
    Track or Attention once viewed." That requires per-item view-tracking
    (a ``viewed``/``last_seen`` field) which the Memory Layer schema does
    not provide in Phase-2. Emitting New as merely "created since last
    close" would make every new item permanently New with no revert — a
    broken state, worse than not using it. So the live assembler produces
    Attention / Scheduled / On Track from real deadline state; ``New``
    stays reserved in the vocabulary (Section 5, DashboardStatus.NEW)
    pending a view-tracking field. This is a deliberate, cited limitation,
    not a silent omission.
    """
    dl = _parse_deadline(node)
    if dl is not None:
        if dl <= now + timedelta(days=7):
            return DashboardStatus.ATTENTION      # due within 7 days or overdue
        return DashboardStatus.SCHEDULED          # future dated item
    return DashboardStatus.ON_TRACK


_STATUS_RANK = {
    DashboardStatus.ATTENTION: 0,
    DashboardStatus.SCHEDULED: 1,
    DashboardStatus.NEW: 2,
    DashboardStatus.ON_TRACK: 3,
}


async def build_dashboard(db: AsyncSession) -> Dashboard:
    """Assemble the whole surface from live Memory-Layer reads."""
    now = datetime.now(timezone.utc)

    # --- Header (6.1) — three indicators, each bound to real state ---------
    session_active = (
        await db.execute(
            select(func.count()).select_from(Session).where(Session.status == SessionStatus.ACTIVE)
        )
    ).scalar_one() > 0
    # Phase-2 has no external connectors; the freshness signal is bound to the
    # nearest live, checkable proxy: a skill invocation the watchdog marked
    # 'timeout'. (True connector freshness is Phase-3 — flagged in HANDOFF.)
    stale = (
        await db.execute(
            select(func.count()).select_from(SkillInvocationLog)
            .where(SkillInvocationLog.status == "timeout")
        )
    ).scalar_one() > 0

    # --- Zone 4: Approvals (6.5) — pending L3/L4 requests, newest first -----
    approval_rows = (
        await db.execute(
            select(PendingEscalation)
            .where(
                PendingEscalation.escalation_type == EscalationType.CLARIFICATION,
                PendingEscalation.status == EscalationStatus.PENDING,
            )
            .order_by(desc(PendingEscalation.created_at))
        )
    ).scalars().all()
    cards = [
        ApprovalCard(
            action=(r.content or {}).get("title", "Action awaiting approval"),
            pillar=(r.content or {}).get("pillar"),
            description=(r.content or {}).get("description", ""),
            action_log_id=(r.content or {}).get("action_log_id"),
        )
        for r in approval_rows
    ]
    approvals = Approvals(cards=cards)

    header = HeaderIndicators(
        session_active=session_active,
        source_freshness_stale=stale,
        pending_reviews=len(cards),   # the Approvals count drives the header badge
    )

    # --- Zone 3: Pillars (6.4) — six tiles, fixed order --------------------
    tiles: list[PillarTile] = []
    for pillar in PILLAR_ORDER:
        nodes = await read_pillar_nodes([pillar], db)
        scored = sorted(
            ((n, _node_status(n, now)) for n in nodes),
            key=lambda ns: (_STATUS_RANK[ns[1]], -(ns[0].created_at.timestamp() if ns[0].created_at else 0)),
        )
        items = [PillarItem(text=n.title, status=st) for n, st in scored[:5]]
        if items:
            tile_status = min((it.status for it in items), key=lambda s: _STATUS_RANK[s])
            top = items[0].text
        else:
            tile_status = DashboardStatus.ON_TRACK
            top = "Nothing needs attention"   # a calm empty pillar, not a gap
        tiles.append(PillarTile(
            pillar=pillar, name=PILLAR_LABEL[pillar], icon=PILLAR_ICON[pillar],
            top_item=top, status=tile_status, items=items,
        ))
    pillars = Pillars(tiles=tiles)

    # --- Zone 2: Today (6.3) — deadline-driven brief, priority order -------
    dated = (
        await db.execute(
            select(Node).where(Node.status == NodeStatus.ACTIVE, Node.metadata_["deadline"].isnot(None))
        )
    ).scalars().all()
    dated_items: list[tuple[datetime, TodayItem]] = []
    for n in dated:
        dl = _parse_deadline(n)
        if dl is None:
            continue
        status = DashboardStatus.ATTENTION if dl <= now + timedelta(days=7) else DashboardStatus.SCHEDULED
        dated_items.append((dl, TodayItem(
            what=n.title, why=f"Due {dl.date().isoformat()}", status=status,
        )))
    dated_items.sort(key=lambda di: di[0])   # earliest deadline first (brief's own ordering)
    today_items = [ti for _, ti in dated_items]
    today = Today(items=today_items[:7], more_count=max(0, len(today_items) - 7))

    # --- Zone 5: Memory & Evidence (6.6) — recent nodes --------------------
    recent = (
        await db.execute(
            select(Node).where(Node.status == NodeStatus.ACTIVE)
            .order_by(desc(Node.created_at)).limit(10)
        )
    ).scalars().all()
    mem_rows: list[MemoryRow] = []
    for n in recent:
        ents = (
            await db.execute(
                select(func.count()).select_from(NodeLink).where(NodeLink.source_id == n.id)
            )
        ).scalar_one()
        mem_rows.append(MemoryRow(description=n.title, entities=int(ents)))

    # Read-only trust-maturity evidence (Foundation §16 dashboard content;
    # surfaces surface_advancement_evidence for the NEXT ladder step). It
    # advances nothing — no /advance endpoint (Phase 3).
    from aether.memory.trust_state import _LADDER, current_trust_stage, surface_advancement_evidence

    stage = await current_trust_stage(db)
    next_stage = _LADDER.get(stage)
    if next_stage is not None:
        ev = await surface_advancement_evidence(next_stage, db)
        trust_evidence = TrustEvidenceView(
            current_stage=stage, next_stage=next_stage, ready=ev.met,
            signals=[TrustSignal(name=s.name, value=s.value, threshold=s.threshold, met=s.met)
                     for s in ev.signals],
        )
    else:
        trust_evidence = TrustEvidenceView(current_stage=stage, next_stage=None, ready=False)
    memory = Memory(rows=mem_rows, trust_evidence=trust_evidence)

    return Dashboard(
        header=header,
        input_bar=InputBar(suggestions=list(_SUGGESTIONS)),
        today=today,
        pillars=pillars,
        approvals=approvals,
        memory=memory,
        files=Files(),
    )


@router.get("", response_model=Dashboard)
async def get_dashboard(db: AsyncSession = Depends(get_db)) -> Dashboard:
    return await build_dashboard(db)


@router.get("/view", response_class=HTMLResponse)
async def get_dashboard_view(db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    dash = await build_dashboard(db)
    return HTMLResponse(content=render_dashboard_html(dash))


# --------------------------------------------------------------------------- #
# Server-rendered view. Global rules (Section 4) enforced in CSS: dark theme,
# one accent color (only for .attention), flat surfaces (no shadow/gradient/
# glow/blur), no animation of any kind (no @keyframes), one command input +
# one search input, no desktop scroll. Section 7 exclusions absent by
# construction (no <img>/avatar, no orb/pulse, no FAB/palette).
# --------------------------------------------------------------------------- #

_CSS = """
:root { --bg:#0d0f12; --panel:#14171c; --text:#e6e8ea; --muted:#8b9199;
        --border:#262b31; --accent:#ff7a45; }
* { box-sizing:border-box; }
html, body { height:100vh; margin:0; overflow:hidden; }  /* no desktop scroll */
/* Flex column so header/chips/grid/files always fit the viewport with no
   overflow and no clipped zone — replaces fragile pixel-math heights. */
body { background:var(--bg); color:var(--text);
       font-family: ui-sans-serif, system-ui, sans-serif;
       display:flex; flex-direction:column; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%;
       border:1px solid var(--muted); vertical-align:middle; }
.dot.filled { background:var(--muted); }
.attention { color:var(--accent); }
.attention .dot, .dot.attention { background:var(--accent); border-color:var(--accent); }
.on_track .dot, .dot.on_track { background:var(--muted); border-color:var(--muted); }
.new .dot, .dot.new { background:transparent; border-color:var(--muted); }
.scheduled .dot, .dot.scheduled { background:transparent; border-color:var(--muted); }
header { flex:0 0 auto; display:flex; align-items:center; gap:16px; padding:10px 16px;
         border-bottom:1px solid var(--border); }
.chips { flex:0 0 auto; padding:4px 16px; color:var(--muted); font-size:12px; }
.wordmark { font-weight:700; letter-spacing:1px; }
#input-bar { flex:1; background:var(--panel); border:1px solid var(--border);
             color:var(--text); padding:8px 12px; }
.indicators { display:flex; gap:12px; align-items:center; }
.badge { background:var(--accent); color:#000; border-radius:8px; padding:0 6px; font-size:12px; }
.grid { flex:1 1 auto; min-height:0; display:grid; grid-template-columns:25% 45% 30%; }
.col { border-right:1px solid var(--border); padding:12px; overflow:hidden; }
.center { display:flex; flex-direction:column; }
.center .panel { flex:1; border-bottom:1px solid var(--border); }
.panel { padding:4px 0; }
.label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:1px; }
.row { padding:6px 0; border-bottom:1px solid var(--border); }
.tile { padding:6px 0; border-bottom:1px solid var(--border); }
.files { flex:0 0 auto; border-top:1px solid var(--border); padding:6px 16px; color:var(--muted); }
#memory-search { width:100%; background:var(--panel); border:1px solid var(--border);
                 color:var(--text); padding:6px 10px; margin-bottom:8px; }
button.approval { background:var(--panel); color:var(--text);
                  border:1px solid var(--border); padding:4px 8px; margin-right:4px; }
"""


def _dot(status: DashboardStatus) -> str:
    return f'<span class="dot {status.value}"></span>'


def render_dashboard_html(dash: Dashboard) -> str:
    h = dash.header
    session_dot = '<span class="dot filled" title="Session active"></span>' if h.session_active \
        else '<span class="dot" title="Session idle"></span>'
    fresh_dot = ('<span class="dot attention" title="A source is stale"></span>'
                 if h.source_freshness_stale
                 else '<span class="dot filled" title="Sources current"></span>')
    badge = f'<span class="badge">{h.pending_reviews}</span>' if h.pending_reviews > 0 else ""

    chips = ""
    if dash.input_bar.show_suggestions:
        chips = " ".join(f'<span class="chip">{c}</span>' for c in dash.input_bar.suggestions)

    today_rows = "".join(
        f'<div class="row {i.status.value}">{_dot(i.status)} <b>{_esc(i.what)}</b><br>'
        f'<span class="label">{_esc(i.why)}</span></div>'
        for i in dash.today.items
    ) or '<div class="row">Nothing for today.</div>'
    more = f'<div class="row"><a href="#pillars">+{dash.today.more_count} more in Pillars</a></div>' \
        if dash.today.more_count else ""

    tiles = "".join(
        f'<div class="tile {t.status.value}" data-pillar="{t.pillar.value}">'
        f'<span class="picon">{_esc(t.icon)}</span> <b>{_esc(t.name)}</b> {_dot(t.status)}<br>'
        f'<span class="label">{_esc(t.top_item)}</span></div>'
        for t in dash.pillars.tiles
    )

    if dash.approvals.cards:
        approvals = "".join(
            f'<div class="row" data-approval>{_esc(c.action)}'
            f'<br><span class="label">{_esc(c.description)}</span><br>'
            f'<button class="approval">Approve</button>'
            f'<button class="approval">Modify</button>'
            f'<button class="approval">Reject</button></div>'
            for c in dash.approvals.cards
        )
    else:
        approvals = f'<div class="row">{_esc(dash.approvals.empty_message)}</div>'

    mem_rows = "".join(
        f'<div class="row">{_esc(m.description)} <span class="label">Entities: {m.entities}</span></div>'
        for m in dash.memory.rows
    ) or '<div class="row">No recent memory.</div>'

    # Read-only trust-maturity evidence block (within the Memory zone).
    te = dash.memory.trust_evidence
    trust_block = ""
    if te is not None:
        if te.next_stage:
            status_cls = "attention" if te.ready else "on_track"
            sigs = "".join(
                f'<div class="row {"on_track" if s.met else ""}">{_dot(DashboardStatus.ON_TRACK if s.met else DashboardStatus.SCHEDULED)} '
                f'{_esc(s.name)} <span class="label">{s.value}/{s.threshold}</span></div>'
                for s in te.signals
            )
            head = (f'Trust maturity: {_esc(te.current_stage)} '
                    f'→ {_esc(te.next_stage)} — '
                    f'{"evidence met (awaiting sign-off)" if te.ready else "evidence not yet met"}')
            trust_block = (f'<div class="row {status_cls}"><b>{head}</b></div>{sigs}'
                           f'<div class="row"><span class="label">{_esc(te.note)}</span></div>')
        else:
            trust_block = (f'<div class="row"><b>Trust maturity: {_esc(te.current_stage)} '
                           f'(ladder ceiling)</b></div>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aether</title><style>{_CSS}</style></head>
<body>
<header>
  <span class="wordmark">AETHER</span>
  <input id="input-bar" type="text" placeholder="{dash.input_bar.placeholder}" />
  <div class="indicators" data-zone="header">
    {session_dot}{fresh_dot}{badge}
  </div>
</header>
<div class="chips" data-zone="input_bar">{chips}</div>
<div class="grid">
  <div class="col" data-zone="today"><div class="label">Today</div>{today_rows}{more}</div>
  <div class="col center">
    <div class="panel" id="pillars" data-zone="pillars"><div class="label">Pillars</div>{tiles}</div>
    <div class="panel" data-zone="memory"><div class="label">Memory &amp; Evidence</div>
      <input id="memory-search" type="search" placeholder="Ask the Memory Layer" />{trust_block}{mem_rows}</div>
  </div>
  <div class="col" data-zone="approvals"><div class="label">Approvals</div>{approvals}</div>
</div>
<div class="files" data-zone="files">{dash.files.strip_text}</div>
</body></html>"""
