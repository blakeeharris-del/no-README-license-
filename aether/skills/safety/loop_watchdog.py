"""
aether.skills.safety.loop_watchdog
=====================================

Per Section 2's repo-structure comment: "NOTE: watchdog class lives in
loops/". This file exists only so ``skills/safety/`` has the file
Section 2 lists there; the actual ``LoopWatchdog`` implementation is
``aether.loops.watchdog.LoopWatchdog`` (Step 13), which is what
``GoalLoop`` and the FastAPI lifespan actually import and use.
"""

from __future__ import annotations

from aether.loops.watchdog import LoopWatchdog

__all__ = ["LoopWatchdog"]
