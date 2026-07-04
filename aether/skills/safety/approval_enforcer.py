"""
aether.skills.safety.approval_enforcer
==========================================

Structural placeholder, not a separately-invoked Phase-0 skill. As
with ``rollback_executor.py``, Section 2's repo structure lists this
file but Section 1.3's "11 total" Phase-0 skill count does not include
it, and Section 15 gives it no numbered spec.

Foundation lists "Approval Enforcer (Safety skill)" as part of the
enforcement chain for INV-01 and INV-05. In Phase-0's actual code, that
enforcement is already implemented directly:
``aether.invariants.guards.assert_has_user_approval`` checks for a
``user_confirmed=True`` action_log row (INV-05), and the Action
Gateway stub (Section 15, SKILL-19) calls it as a precondition before
returning ``mock_executed``. This module re-exports that guard under
the name Section 2 expects, rather than duplicating its logic in a
second implementation that could drift out of sync with the first.
"""

from __future__ import annotations

from aether.invariants.guards import assert_has_user_approval

__all__ = ["assert_has_user_approval"]
