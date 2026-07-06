"""
aether.agents.decision_protocol  (DEPRECATED — superseded in Phase-1)
=====================================================================

Phase-0 parked a structural placeholder here (a bare DecisionPackage
shell that MasterAgent never called). Foundation §10.7.1 classifies the
Decision Protocol as an *executive skill*, so the real Phase-1
implementation lives at ``aether.skills.executive.decision_protocol``
(EC-27, Discrepancy C). Nothing imported this module; it is retained
only as a redirect so any stray reference resolves to the real skill.

This re-export (agents -> skills) is layering-legal: agents/ may import
from skills/.
"""

from __future__ import annotations

from aether.skills.executive.decision_protocol import run_decision_protocol

__all__ = ["run_decision_protocol"]
