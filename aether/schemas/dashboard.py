"""
aether.schemas.dashboard
==========================

The Level 2 Dashboard data model (AETHER_DASHBOARD_SPEC_v1.0). This is
the "view over the memory layer" §16 describes — a presentation contract,
no new system behavior or data model. Exactly the six zones of Section 2,
the four status states of Section 5, and the header of Section 6.1 — no
more, no less.

Section 7 exclusions that are representable in the data model are absent
by construction: there is no avatar/persona field (Rule "Persona"), no
second command-input field (Rule "Input"), and the status vocabulary is a
closed enum of exactly four states (no glyph outside Section 5). Purely
visual exclusions (orb/pulse animation, drop shadows, desktop scroll) live
in the rendered view, not this model.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, Field

from aether.models.enums import PillarName


class DashboardStatus(str, Enum):
    """Section 5 Status Vocabulary — exactly four states. No zone invents
    its own status language; no glyph outside these four (Section 7)."""

    ATTENTION = "attention"    # requires the user's judgment/action this session
    ON_TRACK = "on_track"      # monitored, no action needed
    NEW = "new"                # surfaced since last session close; not yet seen
    SCHEDULED = "scheduled"    # a future, dated item; no current action


# Section 6.4: fixed pillar order — never re-sorted by urgency, so spatial
# memory of the grid stays reliable session to session.
PILLAR_ORDER: list[PillarName] = [
    PillarName.LEGAL,
    PillarName.PERSONAL_FINANCE,
    PillarName.CAREER,
    PillarName.BUSINESS,
    PillarName.HEALTH,
    PillarName.RELATIONSHIPS,
]

# Rule "Iconography": one icon per pillar, used consistently everywhere.
PILLAR_ICON: dict[PillarName, str] = {
    PillarName.LEGAL: "§",
    PillarName.PERSONAL_FINANCE: "$",
    PillarName.CAREER: "↑",
    PillarName.BUSINESS: "▦",
    PillarName.HEALTH: "+",
    PillarName.RELATIONSHIPS: "◇",
}

PILLAR_LABEL: dict[PillarName, str] = {
    PillarName.LEGAL: "Legal",
    PillarName.PERSONAL_FINANCE: "Personal Finance",
    PillarName.CAREER: "Career",
    PillarName.BUSINESS: "Business",
    PillarName.HEALTH: "Health",
    PillarName.RELATIONSHIPS: "Relationships",
}


# --- Zone 1: Input Bar (6.2) ------------------------------------------------
class InputBar(BaseModel):
    placeholder: str = "Type or speak a command."
    # First-load suggested chips (6.2). Static text buttons; collapse after
    # the first command. Not a second input control (Rule "Input").
    suggestions: list[str] = Field(default_factory=list)
    show_suggestions: bool = True


# --- Header (6.1) — three indicators, each bound to real system state -------
class HeaderIndicators(BaseModel):
    session_active: bool           # Session state: filled (active) / outline (idle)
    source_freshness_stale: bool   # Source freshness: gray (current) / attention (stale)
    pending_reviews: int           # Count of Approvals awaiting a decision (0 => no badge)


# --- Zone 2: Today (6.3) ----------------------------------------------------
class TodayItem(BaseModel):
    what: str                      # one line of what it is
    why: str                       # one line of why it matters
    status: DashboardStatus


class Today(BaseModel):
    items: list[TodayItem] = Field(default_factory=list)  # <= 7 visible
    more_count: int = 0            # "+N more in Pillars" when > 7 exist


# --- Zone 3: Pillars (6.4) --------------------------------------------------
class PillarItem(BaseModel):
    text: str
    status: DashboardStatus


class PillarTile(BaseModel):
    pillar: PillarName
    name: str
    icon: str
    top_item: str                  # single most relevant item, or the calm empty line
    status: DashboardStatus
    items: list[PillarItem] = Field(default_factory=list)  # up to 5 on expand


class Pillars(BaseModel):
    tiles: list[PillarTile] = Field(default_factory=list)  # exactly 6, fixed order


# --- Zone 4: Approvals (6.5) ------------------------------------------------
class ApprovalCard(BaseModel):
    action: str
    pillar: str | None = None
    description: str               # Action Gateway plain-language description
    action_log_id: str | None = None   # routes Approve/Modify/Reject to /approve (INV-05)


class Approvals(BaseModel):
    cards: list[ApprovalCard] = Field(default_factory=list)  # newest first
    empty_message: str = "No approvals pending"


# --- Zone 5: Memory & Evidence (6.6) ---------------------------------------
class MemoryRow(BaseModel):
    description: str
    entities: int = 0              # "Entities: N" where relevant


class Memory(BaseModel):
    rows: list[MemoryRow] = Field(default_factory=list)
    search_enabled: bool = True    # the only search surface in the dashboard


# --- Zone 6: File Drop Zone (6.7) ------------------------------------------
class Files(BaseModel):
    collapsed: bool = True         # collapsed strip at rest; never a file list
    strip_text: str = "Drop a file to add it to memory"


class Dashboard(BaseModel):
    """The whole surface: header + the six zones (Section 2). Exactly six
    zones, none added, none omitted."""

    header: HeaderIndicators
    input_bar: InputBar
    today: Today
    pillars: Pillars
    approvals: Approvals
    memory: Memory
    files: Files

    # The six zone names of Section 2 (Input Bar, Today, Pillars, Approvals,
    # Memory, Files) — used by the zone-completeness acceptance check.
    ZONES: ClassVar[tuple[str, ...]] = (
        "input_bar", "today", "pillars", "approvals", "memory", "files",
    )
